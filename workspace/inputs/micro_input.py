#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Microphone Input Device

Connects to a microphone and a speech-to-text backend to get voice transcripts.
The microphone is the machine's ALSA capture device, or — on a machine that has
none, i.e. the sim — a client pushing PCM over ROS (``RosPcmStreamer``).

Four backends, selected by the ``stt_backend`` setting:

- ``elevenlabs_batch`` — ElevenLabs Scribe batch, one POST per utterance (default)
- ``gemini``           — Gemini generateContent, one call per utterance
- ``elevenlabs``       — ElevenLabs Scribe realtime WebSocket
- ``openai``           — OpenAI Realtime transcription sessions

The realtime backends stream audio over a WebSocket and let the vendor do the
voice-activity detection; the batch backends detect utterances locally — Silero
VAD by default, an RMS energy threshold as fallback (``stt_vad_engine``) — and
ship each one whole (see ``brain_client.inputs.batch_stt``), biased toward the
``stt_keyterms`` vocabulary. The microphone, ducking, and reconnect machinery is
shared.

Uses proxy services via self.proxy (injected by InputManager).
"""

import array
import base64
import json
import math
import queue
import re
import shutil
import subprocess
import threading
import time
from collections import deque

from brain_client.brain.transport import pick_rest
from brain_client.common.logging import UniversalLogger
from brain_client.inputs.batch_stt import (
    DEFAULT_KEYTERMS,
    BatchSttSession,
    EnergyDetector,
    Transcriber,
    VoicedDetector,
    elevenlabs_proxy_transcriber,
    gemini_transcriber,
    sanitize_keyterms,
)
from brain_client.inputs.types import InputDevice
from brain_client.inputs.vad import silero_detector

DEFAULT_SAMPLE_RATE = 24_000
DEFAULT_CHANNELS = 1
CHUNK_DURATION_SEC = 0.02

# ElevenLabs names the wire format after the rate; the two must agree or the
# transcript comes out time-warped.
ELEVENLABS_AUDIO_FORMAT = f"pcm_{DEFAULT_SAMPLE_RATE}"

# std_msgs/String of base64 PCM16 mono at DEFAULT_SAMPLE_RATE.
MIC_AUDIO_TOPIC = "/mic/audio"

STT_BACKENDS = frozenset({"elevenlabs_batch", "gemini", "elevenlabs", "openai"})
BATCH_BACKENDS = frozenset({"elevenlabs_batch", "gemini"})
DEFAULT_STT_BACKEND = "elevenlabs_batch"

VAD_ENGINES = frozenset({"silero", "energy"})
DEFAULT_VAD_ENGINE = "silero"
# Reported to the webapp when the vendor endpoints server-side (realtime backends).
VENDOR_VAD_ENGINE = "vendor"

# One vad_status frame per this many mic chunks (20 ms each) — 5 Hz on the wire.
VAD_STATUS_EVERY_CHUNKS = 10
RMS_WINDOW_SECS = 0.5

ELEVENLABS_ERROR_TYPES = frozenset(
    {
        "error",
        "auth_error",
        "quota_exceeded",
        "commit_throttled",
        "unaccepted_terms",
        "rate_limited",
        "queue_overflow",
        "resource_exhausted",
        "session_time_limit_exceeded",
        "input_error",
        "chunk_size_exceeded",
        "transcriber_error",
    }
)


class RollingRms:
    def __init__(self, sample_rate: int, window_secs: float):
        self._target_samples = round(sample_rate * window_secs)
        self._chunks: deque[tuple[int, int]] = deque()
        self._square_sum = 0
        self._sample_count = 0

    def add(self, chunk: bytes) -> float:
        samples = array.array("h", chunk[: len(chunk) - len(chunk) % 2])
        square_sum = sum(sample * sample for sample in samples)
        sample_count = len(samples)
        self._chunks.append((square_sum, sample_count))
        self._square_sum += square_sum
        self._sample_count += sample_count
        while self._chunks and self._sample_count - self._chunks[0][1] >= self._target_samples:
            old_square_sum, old_sample_count = self._chunks.popleft()
            self._square_sum -= old_square_sum
            self._sample_count -= old_sample_count
        if not self._sample_count:
            return 0.0
        return math.sqrt(self._square_sum / self._sample_count) / 32768.0


class MicroInput(InputDevice):
    """
    Microphone input device.

    Connects to a realtime STT backend via proxy (self.proxy.<backend>.realtime).
    Config comes from self.proxy.config (set by InputManagerNode).

    Supports "ducking" - suppresses audio while robot is speaking.
    Automatically reconnects if WebSocket connection is lost.
    """

    def __init__(self):
        super().__init__()
        self.mic = None
        self.client = None
        self._backend = DEFAULT_STT_BACKEND
        self._vad_engine = DEFAULT_VAD_ENGINE
        self._vad_detector = None
        self._last_transcript = ""
        self._stop_evt = threading.Event()
        self._audio_thread = None
        self._is_robot_talking = False  # For ducking (mic-specific)
        self._ducking_started_at = None
        self._reconnect_thread = None
        self._is_connected = False
        self._reconnect_delay = 1  # Start with 1 second
        self._max_reconnect_delay = 30  # Max 30 seconds between retries
        self._realtime_speech_started_at = None
        self._realtime_speech_stopped_at = None
        self._rolling_rms = RollingRms(DEFAULT_SAMPLE_RATE, RMS_WINDOW_SECS)
        self._rms_level = 0.0
        self._audio_device_id = "unavailable"
        self._audio_device_name = "No capture source"
        # Initialize logger wrapper (will be updated when set_logger is called)
        self.logger = UniversalLogger(enabled=False)

    def set_logger(self, logger):
        """Wrap the provided logger with UniversalLogger."""
        super().set_logger(logger)
        # Wrap the logger so we can call methods unconditionally
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)

    @property
    def name(self) -> str:
        return "micro"

    def set_tts_playing(self, is_playing: bool):
        """
        Called when TTS (text-to-speech) status changes.

        Implements "ducking" - suppressing mic input while robot speaks.

        Args:
            is_playing: True if robot is speaking, False otherwise
        """
        if is_playing == self._is_robot_talking:
            return
        self._is_robot_talking = is_playing
        if is_playing:
            self._ducking_started_at = time.monotonic()
            self._emit_speech_debug("ducking_started")
            return
        duration_ms = (
            round((time.monotonic() - self._ducking_started_at) * 1000)
            if self._ducking_started_at is not None
            else None
        )
        self._ducking_started_at = None
        self._emit_speech_debug("ducking_ended", duration_ms=duration_ms)

    def on_open(self):
        """Start the audio source and connect to the STT backend.

        Failures must propagate: InputDeviceManager clears the active flag only
        when this raises, and a device left active is never reopened.
        """
        if not self.proxy:
            raise RuntimeError("no STT configuration (the proxy client was never created)")

        try:
            self._rolling_rms = RollingRms(DEFAULT_SAMPLE_RATE, RMS_WINDOW_SECS)
            self._rms_level = 0.0
            self.mic = self._start_audio_source()
            self.logger.info(f"🎙️ Microphone started (rate: {DEFAULT_SAMPLE_RATE}, channels: {DEFAULT_CHANNELS})")
            self._connect_via_proxy()
        except Exception as e:
            self.logger.error(f"❌ Failed to start microphone: {e}")
            self.on_close()  # release the device/socket so the retry starts clean
            raise

    def _start_audio_source(self):
        device = self._detect_audio_device()

        # No arecord at all, rather than arecord listing no cards: a robot whose
        # microphone did not enumerate still has a working `default` to reach for.
        if shutil.which("arecord") is None and self.node is not None:
            self._audio_device_id = MIC_AUDIO_TOPIC
            self._audio_device_name = "Browser microphone"
            mic = RosPcmStreamer(self.node, self.logger)
            mic.start()
            return mic

        self._audio_device_id = device or "default"
        if device is None:
            self._audio_device_name = "ALSA default"
        self.logger.info(f"🎙️ Using audio device: {self._audio_device_id}")
        mic = ArecordStreamer(self.logger)
        mic.start(device=self._audio_device_id, sample_rate=DEFAULT_SAMPLE_RATE, channels=DEFAULT_CHANNELS)
        return mic

    def _on_elevenlabs_message(self, ws, message: str):
        """Handle incoming messages from the ElevenLabs Scribe realtime API."""
        try:
            event = json.loads(message)
        except Exception:
            self.logger.error(f"Failed to parse message: {message[:200]}")
            return

        etype = event.get("message_type")

        if etype == "committed_transcript":
            text = event.get("text", "")
            if text and self.is_active():
                self._emit_realtime_transcript_debug(text)
                self._on_transcript(text)
        elif etype == "partial_transcript":
            self._mark_realtime_speech_started()
        elif etype == "session_started":
            self.logger.info(f"📋 Scribe session started: {event.get('config', {})}")
        elif etype == "insufficient_audio_activity":
            pass  # expected whenever the room is quiet — not worth a log line
        elif etype in ELEVENLABS_ERROR_TYPES:
            self.logger.error(f"❌ ElevenLabs error ({etype}): {event.get('error', event)}")
        else:
            self.logger.info(f"📨 ElevenLabs event: {etype}")

    def _on_elevenlabs_open(self):
        """Scribe takes its whole session config in the connect URL — nothing to send."""
        self.logger.info("📤 WebSocket opened (Scribe session configured via query params)")

    def _on_openai_message(self, ws, message: str):
        """Handle incoming messages from OpenAI Realtime API."""
        try:
            event = json.loads(message)
        except Exception:
            self.logger.error(f"Failed to parse message: {message[:200]}")
            return

        etype = event.get("type")

        # Event type to handler mapping
        event_handlers = {
            "session.updated": lambda e: self.logger.info(
                f"📋 Session updated - transcription: {e.get('session', {}).get('input_audio_transcription', {})}, "
                f"turn_detection: {e.get('session', {}).get('turn_detection', {})}"
            ),
            "input_audio_buffer.speech_started": lambda e: self._mark_realtime_speech_started(),
            "input_audio_buffer.speech_stopped": lambda e: self._mark_realtime_speech_stopped(),
            "conversation.item.input_audio_transcription.completed": lambda e: (
                self._on_realtime_transcript(e.get("transcript", ""))
                if e.get("transcript") and self.is_active()
                else None
            ),
            "error": lambda e: (
                self.logger.error(
                    f"❌ OpenAI error: {e.get('error', {}).get('code', '')} - "
                    f"{e.get('error', {}).get('message', '')} "
                    f"(param: {e.get('error', {}).get('param', '')})"
                )
                if e.get("error", {}).get("code") != "input_audio_buffer_commit_empty"
                else None
            ),
        }

        # Execute handler if exists, otherwise log unknown event type
        handler = event_handlers.get(etype)
        if handler:
            handler(event)
        else:
            self.logger.info(f"📨 OpenAI event: {etype}")

    def _on_openai_open(self):
        """Handle WebSocket open event - send session configuration."""
        cfg = self.proxy.config
        transcribe_model = cfg.get("openai_transcribe_model", "gpt-4o-mini-transcribe")
        language = self._stt_language()
        vad_threshold = float(cfg.get("stt_realtime_vad_threshold", 0.3))
        silence_secs = float(cfg.get("stt_realtime_vad_silence_secs", 0.7))

        self.logger.info("📤 WebSocket opened, sending session.update...")
        session_update = {
            "type": "session.update",
            "session": {
                "input_audio_format": "pcm16",
                "input_audio_transcription": {"model": transcribe_model, "language": language},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": vad_threshold,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": int(silence_secs * 1000),
                    "create_response": False,
                },
                "instructions": "Transcribe user audio only; do not reply.",
            },
        }
        self.logger.info(f"📤 Session config: model={transcribe_model}, vad_threshold={vad_threshold}")
        self.client.send_json(session_update)
        self.logger.info("📤 session.update sent")

    def _on_ws_error(self, error):
        """Handle WebSocket error event."""
        self.logger.error(f"[ws error] {error}")

    def _on_ws_close(self):
        """Handle WebSocket close event - trigger reconnection."""
        self.logger.warning("WebSocket closed")
        self._is_connected = False

        # Don't reconnect if we're shutting down
        if self._stop_evt.is_set():
            return
        self._emit_speech_debug("connection_lost")

        # Start reconnection in background thread
        self._schedule_reconnect()

    def _stt_language(self) -> str:
        # `or` (not a .get default): a blank setting must fall back too, or it
        # reaches the wire as an empty language code and the session is refused.
        return str(self.proxy.config.get("stt_language") or "en")

    def _stt_keyterms(self) -> list[str]:
        """Vocabulary the batch backends bias toward; an empty list disables biasing."""
        configured = self.proxy.config.get("stt_keyterms", DEFAULT_KEYTERMS)
        terms = sanitize_keyterms(configured)
        if len(terms) != len(configured):
            self.logger.warning(f"⚠️ Dropped {len(configured) - len(terms)} stt_keyterms ElevenLabs would reject")
        return terms

    def _connect_via_proxy(self):
        """Connect to the configured STT backend via proxy."""
        backend = str(self.proxy.config.get("stt_backend") or DEFAULT_STT_BACKEND).strip().lower()
        if backend not in STT_BACKENDS:
            self.logger.error(
                f"❌ Unknown stt_backend {backend!r} — using {DEFAULT_STT_BACKEND!r} (options: {sorted(STT_BACKENDS)})"
            )
            backend = DEFAULT_STT_BACKEND
        if not self.proxy.is_available() and backend != "gemini":
            self.logger.error(
                f"❌ {backend!r} needs the Innate proxy (INNATE_SERVICE_KEY) — falling back to 'gemini' (GEMINI_API_KEY)"
            )
            backend = "gemini"
        self._backend = backend

        if self._backend == "elevenlabs_batch":
            model = self._connect_elevenlabs_batch()
        elif self._backend == "gemini":
            model = self._connect_gemini()
        elif self._backend == "openai":
            model = self._connect_openai()
        else:
            model = self._connect_elevenlabs()

        self.client.start()

        # Start audio streaming thread
        self._start_audio_thread()

        self._is_connected = True
        self._reconnect_delay = 1  # Reset delay on successful connection
        self.logger.info(f"✅ Connected to {self._backend} STT (model: {model})")
        self._send_vad_status()  # so the panel names the backend before any audio arrives

    def _connect_elevenlabs_batch(self) -> str:
        """Start a batch-transcription session on ElevenLabs Scribe. Returns the model id."""
        model = self.proxy.config.get("elevenlabs_batch_stt_model", "scribe_v2")
        transcriber = elevenlabs_proxy_transcriber(self.proxy, model, self._stt_language(), self._stt_keyterms())
        self._start_batch_session(transcriber, model)
        return model

    def _connect_gemini(self) -> str:
        """Start a batch-transcription session on Gemini. Returns the model id."""
        model = self.proxy.config.get("gemini_stt_model", "gemini-3.6-flash")

        rest = pick_rest(self.proxy)
        if rest is None:
            raise RuntimeError("no Gemini access: proxy unavailable and GEMINI_API_KEY unset")

        self._start_batch_session(gemini_transcriber(rest, model, self._stt_language(), self._stt_keyterms()), model)
        return model

    def _start_batch_session(self, transcriber: Transcriber, model: str) -> None:
        cfg = self.proxy.config
        silence_secs = float(cfg.get("stt_vad_silence_secs", 0.5))
        is_voiced, engine = self._make_vad(cfg)
        self.logger.info(f"📤 Batch STT config: model={model}, vad={engine}, silence={silence_secs}s")
        self.client = BatchSttSession(
            transcriber=transcriber,
            sample_rate=DEFAULT_SAMPLE_RATE,
            is_voiced=is_voiced,
            silence_secs=silence_secs,
            on_transcript=self._on_transcript_if_active,
            logger=self.logger,
            on_debug=self._on_batch_debug,
        )

    def _make_vad(self, cfg: dict) -> tuple[VoicedDetector, str]:
        """Build the voiced detector for the batch endpointer: (detector, engine name)."""
        engine = str(cfg.get("stt_vad_engine") or DEFAULT_VAD_ENGINE).strip().lower()
        if engine not in VAD_ENGINES:
            self.logger.error(
                f"❌ Unknown stt_vad_engine {engine!r} — using {DEFAULT_VAD_ENGINE!r} (options: {sorted(VAD_ENGINES)})"
            )
            engine = DEFAULT_VAD_ENGINE
        detector: VoicedDetector | None = None
        if engine == "silero":
            threshold = float(cfg.get("stt_vad_threshold", 0.2))
            detector = silero_detector(threshold, DEFAULT_SAMPLE_RATE, self.logger)
            if detector is None:
                engine = "energy"  # silero_detector logged why
        if detector is None:
            detector = EnergyDetector(float(cfg.get("stt_energy_threshold", 0.01)))
        self._vad_detector, self._vad_engine = detector, engine
        return detector, engine

    def _send_vad_status(self) -> None:
        """One vad_status frame for the webapp's voice panel.

        Realtime backends have no local detector — they still send a frame so
        the panel can say the vendor owns endpointing instead of sitting on
        "waiting for VAD telemetry" forever.
        """
        client, detector = self.client, self._vad_detector
        if client is None:
            return
        frame = {
            "kind": "vad_status",
            "backend": self._backend,
            "engine": self._vad_engine,
            "last_transcript": self._last_transcript,
            "capture": "browser" if isinstance(self.mic, RosPcmStreamer) else "hardware",
            "audio_device_id": self._audio_device_id,
            "audio_device_name": self._audio_device_name,
            "rms": round(self._rms_level, 4),
            "ducking": self._is_robot_talking,
        }
        if self._backend not in BATCH_BACKENDS or detector is None:
            self.send_data({**frame, "engine": VENDOR_VAD_ENGINE}, data_type="telemetry")
            return
        self.send_data(
            {
                **frame,
                "threshold": detector.threshold,
                "level": round(detector.level, 4),
                "silero_score": round(detector.level, 4) if self._vad_engine == "silero" else None,
                "silero_paused": self._vad_engine == "silero" and self._is_robot_talking,
                "voiced": detector.voiced,
                **client.status(),
            },
            data_type="telemetry",
        )

    def _connect_elevenlabs(self) -> str:
        """Open a Scribe realtime session. Returns the model id."""
        self.logger.info("🔗 Connecting to ElevenLabs Scribe via proxy...")

        cfg = self.proxy.config
        model = cfg.get("elevenlabs_stt_model", "scribe_v2_realtime")
        vad_threshold = float(cfg.get("stt_realtime_vad_threshold", 0.3))
        silence_secs = float(cfg.get("stt_realtime_vad_silence_secs", 0.7))

        self.logger.info(
            f"📤 Scribe config: model={model}, audio_format={ELEVENLABS_AUDIO_FORMAT}, "
            f"vad_threshold={vad_threshold}, silence={silence_secs}s"
        )
        self.client = self.proxy.elevenlabs.realtime.connect_sync(
            model_id=model,
            audio_format=ELEVENLABS_AUDIO_FORMAT,
            language_code=self._stt_language(),
            commit_strategy="vad",
            vad_threshold=vad_threshold,
            vad_silence_threshold_secs=silence_secs,
            on_message=self._on_elevenlabs_message,
            on_open=self._on_elevenlabs_open,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )
        return model

    def _connect_openai(self) -> str:
        """Open an OpenAI Realtime transcription session. Returns the model id."""
        self.logger.info("🔗 Connecting to OpenAI via proxy...")

        model = self.proxy.config.get("openai_realtime_model", "gpt-4o-realtime-preview")

        self.client = self.proxy.openai.realtime.connect_sync(
            model=model,
            on_message=self._on_openai_message,
            on_open=self._on_openai_open,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )
        return model

    def _schedule_reconnect(self):
        """Schedule a reconnection attempt in a background thread."""
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return  # Already reconnecting

        def reconnect_loop():
            while not self._stop_evt.is_set() and not self._is_connected:
                self.logger.info(f"🔄 Reconnecting to {self._backend} STT in {self._reconnect_delay}s...")

                # Wait before reconnecting (interruptible)
                if self._stop_evt.wait(timeout=self._reconnect_delay):
                    break  # Stop event was set

                try:
                    # Stop old client if exists
                    if self.client:
                        try:
                            self.client.stop()
                        except:  # noqa: E722
                            pass
                        self.client = None

                    # Stop old audio thread
                    self._stop_evt.set()
                    if self._audio_thread and self._audio_thread.is_alive():
                        self._audio_thread.join(timeout=1.0)
                    self._stop_evt.clear()

                    # Reconnect
                    self._connect_via_proxy()

                    if self._is_connected:
                        self.logger.info("✅ Reconnection successful!")
                        self._emit_speech_debug("connection_restored")
                        break

                except Exception as e:
                    self.logger.error(f"❌ Reconnection failed: {e}")
                    # Exponential backoff
                    self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

        self._reconnect_thread = threading.Thread(target=reconnect_loop, daemon=True)
        self._reconnect_thread.start()

    def _on_transcript_if_active(self, text: str):
        if self.is_active():
            self._on_transcript(text)

    def _on_batch_debug(self, event: dict) -> None:
        audio_queue = getattr(self.mic, "queue", None)
        self.send_data(
            {
                **event,
                **self._speech_debug_context(),
                "audio_queue_chunks": audio_queue.qsize() if audio_queue is not None else None,
                "dropped_audio_chunks": getattr(self.mic, "dropped_chunks", 0),
            },
            data_type="telemetry",
        )

    def _speech_debug_context(self) -> dict:
        detector = self._vad_detector
        cfg = self.proxy.config if self.proxy is not None else {}
        context = {
            "backend": self._backend,
            "engine": self._vad_engine if self._backend in BATCH_BACKENDS else VENDOR_VAD_ENGINE,
            "capture": "browser" if isinstance(self.mic, RosPcmStreamer) else "hardware",
            "audio_device_id": self._audio_device_id,
            "audio_device_name": self._audio_device_name,
            "rms": round(self._rms_level, 4),
            "ducking": self._is_robot_talking,
        }
        if self._backend in BATCH_BACKENDS and detector is not None:
            return {
                **context,
                "vad_threshold": detector.threshold,
                "vad_level": round(detector.level, 4),
                "silero_score": round(detector.level, 4) if self._vad_engine == "silero" else None,
                "silero_paused": self._vad_engine == "silero" and self._is_robot_talking,
                "silence_seconds": float(cfg.get("stt_vad_silence_secs", 0.5)),
            }
        return {
            **context,
            "vad_threshold": float(cfg.get("stt_realtime_vad_threshold", 0.3)),
            "silence_seconds": float(cfg.get("stt_realtime_vad_silence_secs", 0.7)),
        }

    def _emit_speech_debug(self, phase: str, **details) -> None:
        self.send_data(
            {
                "kind": "speech_debug",
                "source": "stt",
                "phase": phase,
                "timestamp": time.time(),
                **self._speech_debug_context(),
                **details,
            },
            data_type="telemetry",
        )

    def _mark_realtime_speech_started(self) -> None:
        if self._realtime_speech_started_at is not None:
            return
        self._realtime_speech_started_at = time.monotonic()
        self._realtime_speech_stopped_at = None
        self.logger.info("🎤 Speech detected")
        self._emit_speech_debug("speech_started")

    def _mark_realtime_speech_stopped(self) -> None:
        stopped_at = time.monotonic()
        audio_seconds = (
            stopped_at - self._realtime_speech_started_at if self._realtime_speech_started_at is not None else None
        )
        self._realtime_speech_stopped_at = stopped_at
        self.logger.info("🔇 Speech stopped")
        self._emit_speech_debug(
            "utterance_closed",
            audio_seconds=round(audio_seconds, 2) if audio_seconds is not None else None,
        )

    def _on_realtime_transcript(self, text: str) -> None:
        self._emit_realtime_transcript_debug(text)
        self._on_transcript(text)

    def _emit_realtime_transcript_debug(self, text: str) -> None:
        now = time.monotonic()
        elapsed_ms = (
            round((now - self._realtime_speech_started_at) * 1000)
            if self._realtime_speech_started_at is not None
            else None
        )
        stop_to_transcript_ms = (
            round((now - self._realtime_speech_stopped_at) * 1000)
            if self._realtime_speech_stopped_at is not None
            else None
        )
        self._realtime_speech_started_at = None
        self._realtime_speech_stopped_at = None
        self._emit_speech_debug(
            "transcript_ready",
            total_ms=elapsed_ms,
            stop_to_transcript_ms=stop_to_transcript_ms,
            characters=len(text),
        )

    def _send_chunk(self, chunk: bytes):
        """Hand one PCM chunk to the backend: fed locally (batch) or framed onto the wire."""
        if self._backend in BATCH_BACKENDS:
            self.client.feed(chunk)
            return
        audio = base64.b64encode(chunk).decode("ascii")
        if self._backend == "openai":
            frame = {"type": "input_audio_buffer.append", "audio": audio}
        else:
            frame = {"message_type": "input_audio_chunk", "audio_base_64": audio}
        self.client.send_json(frame)

    def _start_audio_thread(self):
        """Start the audio streaming thread."""
        self._stop_evt.clear()

        def audio_loop():
            if not self.client.wait_until_connected(timeout=10):
                self.logger.error("WebSocket didn't connect in time")
                return

            self.logger.info("🎧 Audio streaming thread started")

            chunks_sent = 0
            chunks_seen = 0
            empty_count = 0
            ducking_logged = False
            stalled_at = None
            while not self._stop_evt.is_set():
                try:
                    chunk = self.mic.queue.get(timeout=0.1)
                    self._rms_level = self._rolling_rms.add(chunk)
                    empty_count = 0  # Reset on successful get
                    if stalled_at is not None:
                        self._emit_speech_debug(
                            "audio_resumed",
                            stalled_ms=round((time.monotonic() - stalled_at) * 1000),
                        )
                        stalled_at = None
                except queue.Empty:
                    empty_count += 1
                    if empty_count == 50 and not isinstance(self.mic, RosPcmStreamer):
                        self.logger.warning("⚠️ No audio chunks received (queue empty for 5s)")
                        stalled_at = time.monotonic()
                        self._emit_speech_debug("audio_stalled", empty_seconds=5)
                    continue

                try:
                    # Skip sending if not connected (reconnection in progress)
                    if not self._is_connected:
                        continue

                    # Status rides the chunk cadence for every backend — the webapp
                    # marks the voice panel stale after 2.5 s of silence — and keeps
                    # flowing while ducking, so the DUCKING chip stays live too.
                    chunks_seen += 1
                    if chunks_seen % VAD_STATUS_EVERY_CHUNKS == 0:
                        self._send_vad_status()

                    # Skip sending while ducking (robot is speaking)
                    if self._is_robot_talking:
                        if not ducking_logged:
                            self.logger.info("🔇 Ducking active - not sending audio")
                        ducking_logged = True
                        continue
                    ducking_logged = False

                    self._send_chunk(chunk)
                    chunks_sent += 1

                    # Log periodically (much less frequently)
                    if chunks_sent == 100:
                        self.logger.info(f"🎧 Streaming audio ({chunks_sent} chunks)")
                    elif chunks_sent % 2500 == 0:
                        self.logger.info(f"🎧 Audio chunks sent: {chunks_sent}")
                except Exception as e:
                    # Only log if we think we're connected (avoid spam during reconnect)
                    if self._is_connected:
                        self.logger.error(f"Send error: {e}")

        self._audio_thread = threading.Thread(target=audio_loop, daemon=True)
        self._audio_thread.start()

    def on_close(self):
        """Stop microphone and disconnect."""
        self._stop_evt.set()
        self._is_connected = False  # Prevent reconnection attempts

        if self._audio_thread:
            self._audio_thread.join(timeout=1.0)

        if self._reconnect_thread:
            self._reconnect_thread.join(timeout=1.0)

        if self.mic:
            try:
                self.mic.stop()
            except:  # noqa: E722
                pass
            self.mic = None

        if self.client:
            self.client.stop()
            self.client = None

    def _detect_audio_device(self):
        """Detect and list available audio capture devices."""
        devices = []
        try:
            result = subprocess.run(["arecord", "-l"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                pattern = r"card (\d+):.*?\[([^\]]+)\].*?device (\d+):"
                for match in re.finditer(pattern, result.stdout):
                    card_num = match.group(1)
                    card_name = match.group(2)
                    device_num = match.group(3)
                    device_id = f"plughw:{card_num},{device_num}"
                    devices.append({"card": card_num, "device": device_num, "name": card_name, "id": device_id})
        except Exception:
            pass

        # Try to find a suitable microphone device
        preferred_device = None

        self.logger.info(f"🔍 Found {len(devices)} audio devices: {[d['name'] for d in devices]}")

        # Look for USB microphones (usually better quality)
        for dev in devices:
            name_lower = dev["name"].lower()
            if "mic" in name_lower and "usb" in name_lower:
                preferred_device = dev
                break

        # Look for USB Sound Device (common external mic name)
        if not preferred_device:
            for dev in devices:
                name_lower = dev["name"].lower()
                if "sound" in name_lower and ("usb" in name_lower or "pnp" in name_lower):
                    preferred_device = dev
                    break

        # Fall back to any mic
        if not preferred_device:
            for dev in devices:
                if "mic" in dev["name"].lower():
                    preferred_device = dev
                    break

        # Fall back to any USB audio device (but NOT camera)
        if not preferred_device:
            for dev in devices:
                name_lower = dev["name"].lower()
                if "usb" in name_lower and "camera" not in name_lower and "webcam" not in name_lower:
                    preferred_device = dev
                    break

        # Fall back to camera audio (least preferred)
        if not preferred_device:
            for dev in devices:
                if "camera" in dev["name"].lower() or "webcam" in dev["name"].lower():
                    preferred_device = dev
                    break

        # Last resort: use first available device
        if not preferred_device and devices:
            preferred_device = devices[0]

        if preferred_device:
            self._audio_device_name = preferred_device["name"]
            self.logger.info(f"🎙️ Selected audio device: {preferred_device['name']} ({preferred_device['id']})")

        return preferred_device["id"] if preferred_device else None

    def _on_transcript(self, text: str) -> None:
        """Called when transcript is ready."""
        if text:
            self.logger.info(f"🎤 Transcript: {text}")

            self.send_data(text, data_type="chat_in")
            self._last_transcript = text
            self._send_vad_status()


# ========== Audio Streaming Helpers ==========


class RosPcmStreamer:
    """ArecordStreamer's surface, fed by MIC_AUDIO_TOPIC instead of a capture device."""

    def __init__(self, node, logger):
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self.sample_rate = DEFAULT_SAMPLE_RATE
        self.channels = DEFAULT_CHANNELS
        self.dropped_chunks = 0
        self._node = node
        self.logger = logger
        self._sub = None

    def start(self):
        from std_msgs.msg import String

        self._sub = self._node.create_subscription(String, MIC_AUDIO_TOPIC, self._on_audio, 10)
        self.logger.info(f"🎙️ No capture device — listening for browser audio on {MIC_AUDIO_TOPIC}")

    def _on_audio(self, msg):
        try:
            chunk = base64.b64decode(msg.data)
        except (ValueError, TypeError):
            self.logger.error(f"❌ {MIC_AUDIO_TOPIC}: payload is not base64 — dropping chunk")
            return
        try:
            self.queue.put_nowait(chunk)
        except queue.Full:
            self.dropped_chunks += 1

    def stop(self):
        if self._sub is not None:
            self._node.destroy_subscription(self._sub)
            self._sub = None


class ArecordStreamer:
    """Streams audio from ALSA via arecord subprocess."""

    def __init__(self, logger):
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self._proc: subprocess.Popen | None = None
        self.logger = logger
        self.sample_rate = DEFAULT_SAMPLE_RATE
        self.channels = DEFAULT_CHANNELS
        self.dropped_chunks = 0
        self._reader_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self, device: str = "default", sample_rate: int = DEFAULT_SAMPLE_RATE, channels: int = DEFAULT_CHANNELS):
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        # arecord raw PCM 16-bit, stdout
        cmd = [
            "arecord",
            "-D",
            str(device),
            "-f",
            "S16_LE",
            "-r",
            str(self.sample_rate),
            "-c",
            str(self.channels),
            "-t",
            "raw",
            "-q",  # quiet
            "-",
        ]
        self.logger.info(f"🎙️ Starting arecord: {' '.join(cmd)}")
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        if not self._proc or not self._proc.stdout:
            raise RuntimeError("Failed to start arecord process")
        self.logger.info(f"🎙️ arecord process started (pid: {self._proc.pid})")

        def reader():
            try:
                bytes_per_sample = 2
                frame_bytes = int(self.sample_rate * CHUNK_DURATION_SEC * self.channels * bytes_per_sample)
                self.logger.info(f"🎙️ Reader thread started, reading {frame_bytes} bytes per chunk")
                chunks_read = 0
                # bufsize=0 makes stdout a raw FileIO: read(n) returns *at most* n
                # bytes, at any offset. Consumers parse chunks as int16 frames, so
                # a chunk of odd length raises — accumulate and emit whole frames.
                pending = bytearray()
                while not self._stop.is_set():
                    buf = self._proc.stdout.read(frame_bytes - len(pending))
                    if not buf:
                        # Check if process died
                        if self._proc.poll() is not None:
                            stderr = self._proc.stderr.read().decode() if self._proc.stderr else ""
                            self.logger.error(f"❌ arecord died with code {self._proc.returncode}: {stderr}")
                            break
                        time.sleep(0.01)
                        continue
                    pending.extend(buf)
                    if len(pending) < frame_bytes:
                        continue
                    chunk, pending = bytes(pending), bytearray()
                    chunks_read += 1
                    if chunks_read == 1:
                        self.logger.info(f"🎙️ First audio chunk received ({len(chunk)} bytes)")
                    try:
                        self.queue.put_nowait(chunk)
                    except queue.Full:
                        self.dropped_chunks += 1
            except Exception as e:
                self.logger.error(f"arecord reader error: {e}")

        self._reader_thread = threading.Thread(target=reader, daemon=True)
        self._reader_thread.start()

    def stop(self):
        self._stop.set()
        try:
            if self._reader_thread:
                self._reader_thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            if self._proc:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=1.0)
                except Exception:
                    self._proc.kill()
        except Exception:
            pass
