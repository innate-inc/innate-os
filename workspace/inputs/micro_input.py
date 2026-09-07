#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Microphone Input Device

Connects to a microphone and a speech-to-text backend to get voice transcripts.
The microphone is the machine's ALSA capture device, or — on a machine that has
none, i.e. the sim — a client pushing PCM over ROS (``RosPcmStreamer``).

Three backends, selected by the ``stt_backend`` setting:

- ``elevenlabs``       — ElevenLabs Scribe realtime WebSocket (default)
- ``elevenlabs_batch`` — ElevenLabs Scribe batch, one POST per utterance
- ``gemini``           — Gemini generateContent, one call per utterance

Scribe realtime streams over a warm WebSocket and commits utterances from the
same local endpointing the batch backends use — Silero VAD by default, an RMS
energy threshold as fallback (``stt_vad_engine``). Measured 2026-08 on this
mic: vendor-side VAD lands transcripts ~0.6 s later at any silence setting
and trims real words on noisy audio, so every backend endpoints locally.
Batch backends ship each utterance whole (see
``brain_client.inputs.batch_stt``) and are the automatic fallback when the
realtime socket will not come back. Every ElevenLabs path biases toward the
``stt_keyterms`` vocabulary. The microphone, slow-AGC gain, ducking, and
reconnect machinery is shared.

Uses proxy services via self.proxy (injected by InputManager).
"""

import base64
import json
import queue
import re
import shutil
import string
import subprocess
import threading
import time
from collections import deque

import numpy as np

from brain_client.brain.transport import pick_rest
from brain_client.common.logging import UniversalLogger
from brain_client.inputs.batch_stt import (
    DEFAULT_KEYTERMS,
    BatchSttSession,
    Endpointer,
    EnergyDetector,
    Transcriber,
    VoicedDetector,
    elevenlabs_proxy_transcriber,
    gemini_transcriber,
    keyterms_with_name,
    sanitize_keyterms,
)
from brain_client.inputs.speech_lifecycle import SpeechLifecycle
from brain_client.inputs.types import InputDevice
from brain_client.inputs.vad import silero_detector
from brain_client.perception.identity import IdentityMonitor

DEFAULT_SAMPLE_RATE = 24_000
DEFAULT_CHANNELS = 1
CHUNK_DURATION_SEC = 0.02

# ElevenLabs names the wire format after the rate; the two must agree or the
# transcript comes out time-warped.
ELEVENLABS_AUDIO_FORMAT = f"pcm_{DEFAULT_SAMPLE_RATE}"

# std_msgs/String of base64 PCM16 mono at DEFAULT_SAMPLE_RATE.
MIC_AUDIO_TOPIC = "/mic/audio"

STT_BACKENDS = frozenset({"elevenlabs_batch", "gemini", "elevenlabs"})
BATCH_BACKENDS = frozenset({"elevenlabs_batch", "gemini"})
# Scribe realtime since 2026-08: ~0.75 s after the last word vs ~1.1 s for
# batch, same accuracy. Rollback is stt_backend: "elevenlabs_batch" in settings.
DEFAULT_STT_BACKEND = "elevenlabs"

VAD_ENGINES = frozenset({"silero", "energy"})
DEFAULT_VAD_ENGINE = "silero"

# One vad_status frame per this many mic chunks (20 ms each) — 5 Hz on the wire.
VAD_STATUS_EVERY_CHUNKS = 10

# Slow AGC: the far-field capture runs ~-36 dB RMS, and normalizing it was worth
# 3-11 WER points across every engine benchmarked (2026-08).
AGC_TARGET_PEAK = 0.5  # -6 dBFS
# +6 dB of gain per half-life: full 24 dB recovery in ~16 s, +3 dB drift across
# a 2 s speech pause — slow enough not to pump, fast enough to catch the next
# utterance after a loud event.
AGC_RELEASE_HALF_LIFE_S = 4.0
DEFAULT_AGC_MAX_DB = 24.0

# The realtime Scribe session caps keyterms harder than batch: 50 terms, 20 chars.
REALTIME_KEYTERM_MAX = 50
REALTIME_KEYTERM_CHARS = 20
# The vendor force-commits around 36 s of buffered audio; committing first,
# during silence, means that cut can never land mid-word.
SAFETY_COMMIT_SECS = 30.0
# Failed realtime reconnects before the loop settles for the batch backend —
# a robot must keep hearing even when the streaming socket will not come back.
RECONNECTS_BEFORE_BATCH_FALLBACK = 3

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


def _scribe_previous_text(keyterms: "list[str]") -> str:
    """Vocabulary as free session context, greedy within Scribe's ~50-char
    guidance for previous_text."""
    fitted: list[str] = []
    for term in keyterms:
        if len(f"Likely words: {', '.join([*fitted, term])}") > 50:
            continue  # skip the long term — a shorter one later may still fit
        fitted.append(term)
    return f"Likely words: {', '.join(fitted)}" if fitted else ""


_PUNCTUATION = ".,!?;:…-–—"
_LEADING_PUNCTUATION = re.compile(rf"^[{re.escape(_PUNCTUATION)}]+\s+")


def strip_leading_punctuation(text: str) -> str:
    if not text.strip(_PUNCTUATION + string.whitespace):
        return ""
    return _LEADING_PUNCTUATION.sub("", text).strip()


class SlowAgc:
    """Software gain for the quiet far-field mic: instant attack (a loud onset
    can never be pushed past the target), slow release (~16 s back to full
    gain) so speech pauses don't pump. Boost-only — a hot signal passes
    through and int16 saturation is the limiter. Ducked chunks must not pass
    through here: the robot's own voice would slam the gain floor down.
    """

    def __init__(self, max_gain_db: float, chunk_secs: float):
        self._floor = AGC_TARGET_PEAK / 10 ** (max_gain_db / 20)
        self._decay = 0.5 ** (chunk_secs / AGC_RELEASE_HALF_LIFE_S)
        self._peak = AGC_TARGET_PEAK  # unity gain until the room says otherwise

    @property
    def gain(self) -> float:
        # Clamped at unity: boost-only means sub-unity "gain" is never applied,
        # and reporting it would make the energy detector divide a level UP for
        # ~16 s after any loud sound.
        return max(1.0, AGC_TARGET_PEAK / self._peak)

    @property
    def gain_db(self) -> float:
        return float(20 * np.log10(self.gain))

    def __call__(self, chunk: bytes) -> bytes:
        samples = np.frombuffer(chunk[: len(chunk) - len(chunk) % 2], dtype=np.int16)
        if samples.size == 0:
            return chunk
        # int32 before abs: |int16 -32768| wraps back to -32768.
        peak = float(np.abs(samples.astype(np.int32)).max()) / 32768.0
        self._peak = max(peak, self._peak * self._decay, self._floor)
        gain = self.gain
        if gain <= 1.001:
            return chunk
        boosted = np.clip(samples.astype(np.float32) * gain, -32768, 32767)
        return boosted.astype(np.int16).tobytes()


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
        self._agc: SlowAgc | None = None
        self._endpointer: Endpointer | None = None
        self._endpoint_lock = threading.RLock()  # shared by audio, callbacks and lifecycle timers
        self._listening_enabled = False
        self._speech = SpeechLifecycle(self._emit_speech, lock=self._endpoint_lock)
        self._speech_open = None
        self._speech_pending = None
        self._speech_queue = deque()
        self._speech_ambiguous = False
        self._commit_at = 0.0
        self._streamed_bytes = 0
        self._utterance_count = 0
        self._failure_count = 0
        self._sent_keyterms: list[str] = []
        self._scribe_context = ""
        self._scribe_first_chunk = True
        self._last_transcript = ""
        self._stop_evt = threading.Event()  # device shutdown only — never cleared mid-session
        self._audio_stop: threading.Event | None = None  # current audio thread's own stop
        self._audio_thread = None
        self._is_robot_talking = False  # For ducking (mic-specific)
        self._reconnect_thread = None
        self._reconnect_lock = threading.Lock()
        self._reconnecting = False
        self._is_connected = False
        self._reconnect_delay = 1  # Start with 1 second
        self._max_reconnect_delay = 30  # Max 30 seconds between retries
        # Connects and closes without an intervening session_started (a close
        # of a session that did start counts too — it consumed an attempt).
        self._connect_failures = 0
        self._identity = None  # IdentityMonitor, created on first open (needs the node)
        self._warned_dropped_keyterms = False
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

    def set_listening_enabled(self, enabled: bool) -> bool:
        changed = self._listening_enabled != enabled
        self._listening_enabled = enabled
        return changed

    def set_tts_playing(self, is_playing: bool):
        """
        Called when TTS (text-to-speech) status changes.

        Implements "ducking" - suppressing mic input while robot speaks.

        Args:
            is_playing: True if robot is speaking, False otherwise
        """
        if is_playing and not self._is_robot_talking:
            self._commit_before_duck()
        self._is_robot_talking = is_playing

    def _commit_before_duck(self) -> None:
        """The robot is about to talk over an utterance in flight: commit it now.

        Left open, it would be committed mid-duck by the safety commit (the
        sentence splits in two) or glued to the user's next sentence.
        """
        endpointer = self._endpointer
        if self._backend in BATCH_BACKENDS:
            if self._listening_enabled and self.client is not None:
                self.client.flush()
            return
        if endpointer is None:
            return
        with self._endpoint_lock:
            if not endpointer.in_speech:
                return
            closed = endpointer.flush()
            if self._listening_enabled:
                self._queue_utterance(closed)
                return
        self._commit_scribe()

    def on_open(self):
        """Start the audio source and connect to the STT backend.

        Failures must propagate: InputDeviceManager clears the active flag only
        when this raises, and a device left active is never reopened.
        """
        if not self.proxy:
            raise RuntimeError("no STT configuration (the proxy client was never created)")
        if self._identity is None and self.node is not None:
            self._identity = IdentityMonitor(self.node)

        self._stop_evt.clear()  # a reopened device starts unstopped
        self._connect_failures = 0
        max_gain_db = float(self.proxy.config.get("stt_agc_max_db", DEFAULT_AGC_MAX_DB))
        self._agc = SlowAgc(max_gain_db, CHUNK_DURATION_SEC) if max_gain_db > 0 else None

        try:
            self.mic = self._start_audio_source()
            self.logger.info(
                f"🎙️ Microphone started (rate: {DEFAULT_SAMPLE_RATE}, channels: {DEFAULT_CHANNELS}, "
                f"agc: {'off' if self._agc is None else f'≤+{max_gain_db:g} dB'})"
            )
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
            mic = RosPcmStreamer(self.node, self.logger)
            mic.start()
            return mic

        self.logger.info(f"🎙️ Using audio device: {device or 'default'}")
        mic = ArecordStreamer(self.logger)
        mic.start(device=device or "default", sample_rate=DEFAULT_SAMPLE_RATE, channels=DEFAULT_CHANNELS)
        return mic

    def _on_elevenlabs_message(self, ws, message: str, *, lifecycle: bool | None = None, session=None):
        with self._endpoint_lock:
            if session is not None and session is not self._speech:
                return
            self._process_elevenlabs_message(ws, message, lifecycle=lifecycle)

    def _session_callback(self, session, callback, *args):
        with self._endpoint_lock:
            if session is self._speech:
                callback(*args)

    def _process_elevenlabs_message(self, ws, message: str, *, lifecycle: bool | None = None):
        """Handle incoming messages from the ElevenLabs Scribe realtime API."""
        try:
            event = json.loads(message)
        except Exception:
            self.logger.error(f"Failed to parse message: {message[:200]}")
            return

        etype = event.get("message_type")

        if etype == "committed_transcript":
            text = event.get("text", "")
            with self._endpoint_lock:
                # Opted-in sessions send one locally closed utterance at a time.
                token, self._speech_pending = self._speech_pending, None
                if not self._speech_ambiguous:
                    self._speech.finish(token, text)
                    self._send_next_utterance()

            if text:
                self._utterance_count += 1
                latency = f"{round((time.monotonic() - self._commit_at) * 1000)} ms" if self._commit_at else "n/a"
                self.logger.info(f"🎤 Scribe committed in {latency}: {text[:80]!r}")
            if text and self.is_active():
                self._on_transcript(text, lifecycle=lifecycle)
        elif etype == "partial_transcript":
            pass  # interim result — only the committed transcript reaches chat
        elif etype == "session_started":
            self._connect_failures = 0
            self.logger.info(f"📋 Scribe session started: {event.get('config', {})}")
            echoed = event.get("config", {}).get("keyterms") or []
            if len(echoed) < len(self._sent_keyterms):
                self.logger.warning(
                    f"⚠️ Proxy relay collapsed keyterms: {len(echoed)}/{len(self._sent_keyterms)} reached "
                    f"Scribe — previous_text carries the vocabulary until the relay forwards repeated params"
                )
        elif etype == "insufficient_audio_activity":
            self._invalidate_speech()  # expected whenever the room is quiet — not worth a log line
        elif etype in ELEVENLABS_ERROR_TYPES:
            self._invalidate_speech()
            self._failure_count += 1
            self.logger.error(f"❌ ElevenLabs error ({etype}): {event.get('error', event)}")
        else:
            self.logger.info(f"📨 ElevenLabs event: {etype}")

    def _on_elevenlabs_open(self):
        """Scribe takes its whole session config in the connect URL — nothing to send."""
        self.logger.info("📤 WebSocket opened (Scribe session configured via query params)")

    def _on_ws_error(self, error):
        """Handle WebSocket error event."""
        self._invalidate_speech()
        self._failure_count += 1
        self.logger.error(f"[ws error] {error}")

    def _on_ws_close(self):
        """Handle WebSocket close event - trigger reconnection."""
        self._invalidate_speech()
        self.logger.warning("WebSocket closed")
        self._is_connected = False
        self._connect_failures += 1

        # Don't reconnect if we're shutting down
        if self._stop_evt.is_set():
            return

        # Start reconnection in background thread
        self._schedule_reconnect()

    def _stt_language(self) -> str:
        # `or` (not a .get default): a blank setting must fall back too, or it
        # reaches the wire as an empty language code and the session is refused.
        return str(self.proxy.config.get("stt_language") or "en")

    def _stt_keyterms(self) -> list[str]:
        """Bias vocabulary, re-read per utterance so a renamed robot hears its
        new name without an STT restart; an empty list disables biasing."""
        configured = self.proxy.config.get("stt_keyterms", DEFAULT_KEYTERMS)
        terms = sanitize_keyterms(configured)
        if len(terms) != len(configured) and not self._warned_dropped_keyterms:
            self._warned_dropped_keyterms = True
            self.logger.warning(f"⚠️ Dropped {len(configured) - len(terms)} stt_keyterms ElevenLabs would reject")
        identity = self._identity.current if self._identity is not None else None
        return keyterms_with_name(terms, identity.name if identity else None)

    def _connect_via_proxy(self, prefer_batch: bool = False):
        """Connect to the configured STT backend via proxy."""
        backend = str(self.proxy.config.get("stt_backend") or DEFAULT_STT_BACKEND).strip().lower()
        if backend not in STT_BACKENDS:
            self.logger.error(
                f"❌ Unknown stt_backend {backend!r} — using {DEFAULT_STT_BACKEND!r} (options: {sorted(STT_BACKENDS)})"
            )
            backend = DEFAULT_STT_BACKEND
        if prefer_batch and backend == "elevenlabs":
            # The realtime socket would not come back; batch keeps the robot
            # hearing until the next voice-session open retries realtime.
            self.logger.warning("⚠️ Falling back to 'elevenlabs_batch' after repeated realtime failures")
            backend = "elevenlabs_batch"
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
        transcriber = elevenlabs_proxy_transcriber(self.proxy, model, self._stt_language(), self._stt_keyterms)
        self._start_batch_session(transcriber, model)
        return model

    def _connect_gemini(self) -> str:
        """Start a batch-transcription session on Gemini. Returns the model id."""
        model = self.proxy.config.get("gemini_stt_model", "gemini-3.6-flash")

        rest = pick_rest(self.proxy)
        if rest is None:
            raise RuntimeError("no Gemini access: proxy unavailable and GEMINI_API_KEY unset")

        self._start_batch_session(gemini_transcriber(rest, model, self._stt_language(), self._stt_keyterms), model)
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
            on_transcript=lambda text, enabled=self._listening_enabled: self._on_transcript_if_active(
                text, lifecycle=enabled
            ),
            on_speech=self._emit_speech,
            logger=self.logger,
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
            detector = EnergyDetector(float(cfg.get("stt_energy_threshold", 0.01)), gain=self._agc_gain)
        self._vad_detector, self._vad_engine = detector, engine
        return detector, engine

    def _agc_gain(self) -> float:
        return self._agc.gain if self._agc else 1.0

    def _send_vad_status(self) -> None:
        """One vad_status frame for the webapp's voice panel.

        Every backend endpoints locally now; a frame with no detector fields
        only happens in the moment before a session finishes connecting.
        """
        client, detector = self.client, self._vad_detector
        if client is None:
            return
        frame = {
            "kind": "vad_status",
            "backend": self._backend,
            "engine": self._vad_engine,
            "last_transcript": self._last_transcript,
        }
        status = self._endpointing_status(client)
        if detector is None or status is None:
            self.send_data(frame, data_type="telemetry")
            return
        if self._agc is not None:
            frame["gain_db"] = round(self._agc.gain_db, 1)
        self.send_data(
            {
                **frame,
                "threshold": detector.threshold,
                "level": round(detector.level, 4),
                "voiced": detector.voiced,
                "ducking": self._is_robot_talking,
                **status,
            },
            data_type="telemetry",
        )

    def _endpointing_status(self, client) -> "dict | None":
        """Utterance counters for the panel; None until a session is connected.

        ``client`` is the caller's captured reference — re-reading self.client
        here would race the reconnect thread nulling it.
        """
        if self._backend in BATCH_BACKENDS:
            return client.status()
        if self._endpointer is None:
            return None
        return {
            "utterance_open": self._endpointer.in_speech,
            "utterance_secs": round(self._endpointer.utterance_secs, 2),
            "utterances": self._utterance_count,
            "failures": self._failure_count,
        }

    def _connect_elevenlabs(self) -> str:
        """Open a Scribe realtime session committed by the local endpointer. Returns the model id."""
        self.logger.info("🔗 Connecting to ElevenLabs Scribe via proxy...")

        cfg = self.proxy.config
        model = cfg.get("elevenlabs_stt_model", "scribe_v2_realtime")
        silence_secs = float(cfg.get("stt_vad_silence_secs", 0.5))
        filter_background = bool(cfg.get("stt_filter_background_audio", True))
        keyterms = self._realtime_keyterms()

        is_voiced, engine = self._make_vad(cfg)
        # Recreated on every (re)connect: a half-open utterance must not leak
        # across sockets.
        with self._endpoint_lock:
            self._speech.close()
            self._speech = SpeechLifecycle(self._emit_speech, lock=self._endpoint_lock)
            self._speech_open = self._speech_pending = None
            self._speech_queue.clear()
            self._speech_ambiguous = False
            self._endpointer = Endpointer(sample_rate=DEFAULT_SAMPLE_RATE, is_voiced=is_voiced, silence_secs=silence_secs)
            self._streamed_bytes = 0
            self._commit_at = 0.0
            self._sent_keyterms = keyterms
            # previous_text is body content, so it survives the proxy relay that
            # currently collapses the repeated keyterms query params to one.
            self._scribe_context = _scribe_previous_text(keyterms)
            self._scribe_first_chunk = True
            session = self._speech
            enabled = self._listening_enabled
        self.logger.info(
            f"📤 Scribe config: model={model}, commit=manual/{engine}, silence={silence_secs}s, "
            f"keyterms={len(keyterms)}, filter_background={filter_background}"
        )
        self.client = self.proxy.elevenlabs.realtime.connect_sync(
            model_id=model,
            audio_format=ELEVENLABS_AUDIO_FORMAT,
            language_code=self._stt_language(),
            commit_strategy="manual",
            keyterms=keyterms,
            filter_background_audio=filter_background,
            on_message=lambda *args, session=session, enabled=enabled: self._on_elevenlabs_message(
                *args, lifecycle=enabled, session=session
            ),
            on_open=self._on_elevenlabs_open,
            on_error=lambda *args, session=session: self._session_callback(session, self._on_ws_error, *args),
            on_close=lambda *args, session=session: self._session_callback(session, self._on_ws_close, *args),
        )
        return model

    def _realtime_keyterms(self) -> list[str]:
        terms = self._stt_keyterms()
        kept = [t for t in terms if len(t) <= REALTIME_KEYTERM_CHARS][:REALTIME_KEYTERM_MAX]
        if len(kept) != len(terms):
            dropped = sorted(set(terms) - set(kept))
            self.logger.warning(
                f"⚠️ Realtime Scribe caps keyterms at {REALTIME_KEYTERM_CHARS} chars — dropped: {dropped}"
            )
        return kept

    def _schedule_reconnect(self):
        """Schedule a reconnection attempt in a background thread.

        The flag (not thread liveness) decides "already reconnecting": a close
        landing while the previous loop is exiting would see a live thread and
        be dropped, leaving the mic dead with no reconnect running. The loop's
        finally re-checks and respawns instead.
        """
        with self._reconnect_lock:
            if self._reconnecting:
                return
            self._reconnecting = True

        def reconnect_loop():
            try:
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

                        # Stop the old audio thread by its own event — the
                        # device-wide _stop_evt stays untouched, so an on_close
                        # racing this loop is never un-stopped.
                        if self._audio_stop is not None:
                            self._audio_stop.set()
                        if self._audio_thread and self._audio_thread.is_alive():
                            self._audio_thread.join(timeout=1.0)
                        if self._stop_evt.is_set():
                            break  # the device closed while we were tearing down

                        # Reconnect
                        self._connect_via_proxy(prefer_batch=self._connect_failures >= RECONNECTS_BEFORE_BATCH_FALLBACK)

                        if self._is_connected:
                            self.logger.info("✅ Reconnection successful!")
                            break

                    except Exception as e:
                        self._connect_failures += 1
                        self.logger.error(f"❌ Reconnection failed ({self._connect_failures}): {e}")
                        # Exponential backoff
                        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
            finally:
                with self._reconnect_lock:
                    self._reconnecting = False
                    respawn = not self._is_connected and not self._stop_evt.is_set()
            if respawn:
                self._schedule_reconnect()  # a close landed during our last lines

        self._reconnect_thread = threading.Thread(target=reconnect_loop, daemon=True)
        self._reconnect_thread.start()

    def _on_transcript_if_active(self, text: str, *, lifecycle: bool | None = None):
        if self.is_active():
            self._on_transcript(text, lifecycle=lifecycle)

    def _send_chunk(self, chunk: bytes):
        """Hand one PCM chunk to the backend: fed locally (batch) or framed onto the wire."""
        if self._backend in BATCH_BACKENDS:
            self.client.feed(chunk)
            return
        if self._listening_enabled:
            with self._endpoint_lock:
                if self._speech_ambiguous or self._endpointer is None:
                    return
                was_open = self._endpointer.in_speech
                closed = self._endpointer.feed(chunk)
                if not was_open and self._endpointer.in_speech:
                    self._speech_open = self._speech.start()
                if was_open and not self._endpointer.in_speech:
                    self._queue_utterance(closed)
            return
        self._send_scribe_audio(chunk, commit=False)
        if self._endpointer is None:
            return
        with self._endpoint_lock:
            closed = self._endpointer.feed(chunk)
            discarded = self._endpointer.closed_discarded
        # A discarded close (cough, click — under MIN_VOICED_SECS) was still
        # streamed, so commit it out of the vendor buffer too, or it comes back
        # glued to the front of the next real transcript.
        if closed is not None or discarded:
            self._commit_scribe()
        elif self._safety_commit_due() and not self._vad_detector.voiced:
            # Any unvoiced chunk will do: waiting for the utterance to close
            # could put the buffer past the vendor's ~36 s force-commit, and a
            # split at a pause cannot cut mid-word.
            self._commit_scribe()

    def _send_scribe_audio(self, chunk: bytes, commit: bool) -> bool:
        message = {
            "message_type": "input_audio_chunk",
            "audio_base_64": base64.b64encode(chunk).decode("ascii"),
            "commit": commit,
            "sample_rate": DEFAULT_SAMPLE_RATE,
        }
        # Only the session's first chunk may carry context — later chunks with
        # previous_text are rejected outright (so a keep-warm silence frame that
        # happens to open the session must spend it; the vendor gives no other slot).
        if self._scribe_first_chunk:
            self._scribe_first_chunk = False
            if self._scribe_context:
                message["previous_text"] = self._scribe_context
        sent = self.client.send_json(message)
        self._streamed_bytes += len(chunk)
        return sent

    def _emit_speech(self, event: dict) -> None:
        if not self._listening_enabled:
            return
        if event.get("stage") == "finished":
            event = dict(event, text=strip_leading_punctuation(event.get("text", "")))
        self.send_data(event, data_type="speech")
        if event.get("reason") == "timeout" and self._backend not in BATCH_BACKENDS:
            self._invalidate_speech()

    def _invalidate_speech(self) -> None:
        with self._endpoint_lock:
            was_ambiguous = self._speech_ambiguous
            self._speech_ambiguous = True
            self._speech.close()
            self._speech_open = self._speech_pending = None
            self._speech_queue.clear()
            if not was_ambiguous and self._listening_enabled and self.is_active() and not self._stop_evt.is_set():
                self._is_connected = False
                self._schedule_reconnect()  # bounded existing reconnect; old-session text is discarded

    def _queue_utterance(self, pcm: bytes | None) -> None:
        token, self._speech_open = self._speech_open, None
        if pcm is None:
            self._speech.finish(token, reason="discarded")
            return
        self._speech.ended(token)
        if len(self._speech_queue) >= 3:
            dropped, _ = self._speech_queue.popleft()
            self._speech.finish(dropped, reason="backlog_dropped")
        self._speech_queue.append((token, pcm))
        self._send_next_utterance()

    def _send_next_utterance(self) -> None:
        if not self._listening_enabled or self._speech_ambiguous or self._speech_pending is not None:
            return
        if not self._speech_queue:
            return
        token, pcm = self._speech_queue.popleft()
        self._speech_pending = token
        # Same native manual-commit protocol, with bounded local serialization.
        # No silence/safety commits enter this session and confuse correlation.
        for offset in range(0, len(pcm), 4800):
            if not self._send_scribe_audio(pcm[offset : offset + 4800], commit=False):
                self._invalidate_speech()
                return
        if not self._send_scribe_audio(b"", commit=True):
            self._invalidate_speech()

    def _commit_scribe(self) -> None:
        if not self._send_scribe_audio(b"", commit=True):
            # The socket died under us. The vendor buffer dies with the session,
            # so the utterance is unrecoverable — say so instead of losing it
            # silently, and leave the counters for the reconnect to reset.
            self.logger.error("❌ Scribe commit could not be sent (socket down) — utterance lost")
            return
        self._commit_at = time.monotonic()
        self._streamed_bytes = 0

    def _safety_commit_due(self) -> bool:
        return self._streamed_bytes > SAFETY_COMMIT_SECS * DEFAULT_SAMPLE_RATE * 2

    def _keep_warm(self, nbytes: int) -> None:
        # The endpointer is deliberately not fed here: a paused stream must not
        # count as silence (the batch endpointer holds the same invariant).
        # in_speech is False during a duck — _commit_before_duck closed any open
        # utterance — so this can never commit mid-sentence; the guard is belt.
        if self._backend in BATCH_BACKENDS or self.client is None or self._listening_enabled:
            return
        self._send_scribe_audio(b"\x00" * nbytes, commit=False)
        if self._safety_commit_due() and (self._endpointer is None or not self._endpointer.in_speech):
            self._commit_scribe()

    def _start_audio_thread(self):
        """Start the audio streaming thread.

        Each thread gets its own stop event: a predecessor that outlived its
        join stays stopped instead of being resurrected by a shared flag.
        """
        stop = self._audio_stop = threading.Event()

        def audio_loop():
            if not self.client.wait_until_connected(timeout=10):
                self.logger.error("WebSocket didn't connect in time")
                return
            if stop.is_set() or self._stop_evt.is_set():
                return  # stopped while we waited on the handshake

            self.logger.info("🎧 Audio streaming thread started")

            chunks_sent = 0
            chunks_seen = 0
            empty_count = 0
            ducking_logged = False
            while not stop.is_set() and not self._stop_evt.is_set():
                try:
                    chunk = self.mic.queue.get(timeout=0.1)
                    empty_count = 0  # Reset on successful get
                except queue.Empty:
                    empty_count += 1
                    if empty_count == 50:
                        self.logger.warning("⚠️ No audio chunks received (queue empty for 5s)")
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

                    # While ducking (robot is speaking), real audio is dropped —
                    # but the Scribe socket still gets silence so the warm
                    # session never goes cold mid-conversation.
                    if self._is_robot_talking:
                        if not ducking_logged:
                            self.logger.info("🔇 Ducking active - not sending audio")
                        ducking_logged = True
                        self._keep_warm(len(chunk))
                        continue
                    ducking_logged = False

                    self._send_chunk(chunk if self._agc is None else self._agc(chunk))
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
        self._invalidate_speech()
        if self._audio_stop is not None:
            self._audio_stop.set()
        self._is_connected = False  # Prevent reconnection attempts

        # Reconnect thread first — it may still be creating the audio thread
        # this method is about to join.
        if self._reconnect_thread:
            self._reconnect_thread.join(timeout=2.0)

        if self._audio_thread:
            self._audio_thread.join(timeout=1.0)

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
                pattern = r"card (\d+): (\S+) \[([^\]]+)\].*?device (\d+):"
                for match in re.finditer(pattern, result.stdout):
                    card_num = match.group(1)
                    card_id = match.group(2)
                    card_name = match.group(3)
                    device_num = match.group(4)
                    # sysdefault goes through dsnoop, so the teleop WebRTC stream can open the same mic
                    # concurrently (plughw is exclusive and would lock it out) — but it only addresses
                    # device 0, so a card capturing on another device keeps its exact, exclusive address.
                    device_id = f"sysdefault:CARD={card_id}" if device_num == "0" else f"plughw:{card_num},{device_num}"
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
            self.logger.info(f"🎙️ Selected audio device: {preferred_device['name']} ({preferred_device['id']})")

        return preferred_device["id"] if preferred_device else None

    def _on_transcript(self, text: str, *, lifecycle: bool | None = None) -> None:
        """Called when transcript is ready."""
        text = strip_leading_punctuation(text)
        if not text:
            return
        self.logger.info(f"🎤 Transcript: {text}")

        tagged = self._listening_enabled if lifecycle is None else lifecycle
        self.send_data({"text": text, "speech_lifecycle": True} if tagged else text, data_type="chat_in")
        self._last_transcript = text
        self._send_vad_status()


# ========== Audio Streaming Helpers ==========


class RosPcmStreamer:
    """ArecordStreamer's surface, fed by MIC_AUDIO_TOPIC instead of a capture device."""

    def __init__(self, node, logger):
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=100)
        self.sample_rate = DEFAULT_SAMPLE_RATE
        self.channels = DEFAULT_CHANNELS
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
            pass

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
                        pass
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
