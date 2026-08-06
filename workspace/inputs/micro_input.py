#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Microphone Input Device

Connects to microphone hardware and OpenAI's realtime API to get voice transcripts.
This is a pure Python class with NO ROS dependencies.

Uses proxy services via self.proxy (injected by InputManager).
"""

import base64
import json
import queue
import re
import subprocess
import threading
import time
from collections import deque

from brain_client.audio.barge_in import BargeInDetector
from brain_client.common.logging import UniversalLogger
from brain_client.inputs.types import InputDevice

DEFAULT_SAMPLE_RATE = 24_000
DEFAULT_CHANNELS = 1
CHUNK_DURATION_SEC = 0.02


class MicroInput(InputDevice):
    """
    Microphone input device.

    Connects to OpenAI Realtime API via proxy (self.proxy.openai.realtime).
    Config comes from self.proxy.config (set by InputManagerNode).

    Supports "ducking" - suppresses audio while robot is speaking.
    Automatically reconnects if WebSocket connection is lost.
    """

    def __init__(self):
        super().__init__()
        self.mic = None
        self.client = None
        self._stop_evt = threading.Event()
        self._audio_thread = None
        self._is_robot_talking = False  # For ducking (mic-specific)
        self._barge_in: BargeInDetector | None = None
        self._ref_rate = 44100
        self._tts_mic_percent = 50
        self._unduck_until = 0.0
        self._recent_tts: deque[tuple[float, set]] = deque(maxlen=8)
        self._tail_to_send: list[bytes] = []
        self._reconnect_thread = None
        self._is_connected = False
        self._reconnect_delay = 1  # Start with 1 second
        self._max_reconnect_delay = 30  # Max 30 seconds between retries
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
        While ducked, chunks are routed to the barge-in detector instead of
        being dropped, so a human talking over the robot is still noticed.

        Args:
            is_playing: True if robot is speaking, False otherwise
        """
        was = self._is_robot_talking
        self._is_robot_talking = is_playing
        if is_playing != was:
            self._set_capture_gain(ducked=is_playing)
        if was and not is_playing:
            # Reverb rings past aplay exit (resonating room) and queued
            # utterances restart within ~100ms; without this hold-off the tail
            # reaches STT and gets transcribed as user speech (self-talk loop).
            self._unduck_until = time.monotonic() + 0.4
        det = self._barge_in
        if det is None or is_playing or not was:
            return
        # Playback just ended (finished or barge-in kill). Backup end signal in
        # case the ref "end" event was lost, then optionally replay the mic
        # audio captured since the trigger so the first words aren't lost.
        if self.proxy and self.proxy.config.get("barge_in_flush_tail", False):
            self._tail_to_send = det.drain_tail()
        det.on_ref_end(-1)

    def feed_tts_ref(self, event: dict):
        """TTS reference audio event from /tts/ref_audio (routed by the manager)."""
        det = self._barge_in
        if det is None or not self.is_active():
            return
        etype = event.get("e")
        if etype == "start":
            self._ref_rate = int(event.get("rate", 44100))
            if event.get("text"):
                self._recent_tts.append((time.monotonic(), _norm_tokens(str(event["text"]))))
            det.on_ref_start(int(event.get("seq", -1)))
        elif etype == "pcm":
            det.feed_ref(event.get("pcm", b""), self._ref_rate)
        elif etype == "end":
            det.on_ref_end(int(event.get("seq", -1)))

    def note_servo_motion(self):
        """Arm/head servos are moving — their noise at the gripper mic must
        not count as barge-in evidence (called by the manager at joint rate)."""
        det = self._barge_in
        if det is not None:
            det.suppress_hot_until_wall = time.monotonic() + 0.35

    def _on_barge_in_trigger(self, info: dict):
        """Detector callback (audio thread): tell the brain to stop talking."""
        self.send_data(dict(info), data_type="barge_in")

    def on_open(self):
        """Start microphone and connect to OpenAI via proxy."""
        # Check proxy is available
        if not self.proxy or not self.proxy.is_available():
            self.logger.error("❌ Proxy not configured - cannot start microphone input")
            return

        try:
            # Auto-detect and start microphone
            detected_device = self._detect_audio_device()
            if not detected_device:
                detected_device = "default"

            self.logger.info(f"🎙️ Using audio device: {detected_device}")

            # Pass logger to ArecordStreamer
            self.mic = ArecordStreamer(self.logger)
            self.mic.start(
                device=detected_device if detected_device != "default" else "default",
                sample_rate=DEFAULT_SAMPLE_RATE,
                channels=DEFAULT_CHANNELS,
            )

            self.logger.info(f"🎙️ Microphone started (rate: {DEFAULT_SAMPLE_RATE}, channels: {DEFAULT_CHANNELS})")

            self._init_barge_in()
            # a crash mid-TTS could have left the capture gain ducked
            self._set_capture_gain(ducked=False)

            # Connect via proxy
            self._connect_via_proxy()

        except Exception as e:
            self.logger.error(f"❌ Failed to start microphone: {e}")
            import traceback

            traceback.print_exc()

    def _set_capture_gain(self, ducked: bool):
        """Drop the mic capture gain while the robot speaks, restore after.

        At full gain the speaker-to-mic coupling clips the mic during TTS,
        which blinds barge-in detection; but full gain is needed the rest of
        the time for STT sensitivity (the gripper mic is weak). The two needs
        never overlap in time, so switch at utterance boundaries. Nothing is
        sent to STT while ducked, so OpenAI never sees the gain steps.
        """
        if self._barge_in is None:
            return
        pct = int(self._tts_mic_percent if ducked else 100)
        try:
            subprocess.Popen(
                ["amixer", "-q", "-c", "Light", "sset", "Mic", f"{pct}%"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            self.logger.debug(f"capture gain set failed: {e}")

    def _init_barge_in(self):
        """Create the barge-in detector once; coupling gains persist across cycles."""
        if self._barge_in is not None:
            return
        cfg = self.proxy.config if self.proxy else {}
        if not cfg.get("barge_in_enabled", True):
            self.logger.info("🙋 Barge-in detection disabled by config")
            return
        self._tts_mic_percent = int(cfg.get("barge_in_tts_mic_percent", 50))
        try:
            self._barge_in = BargeInDetector(
                logger=self.logger,
                on_trigger=self._on_barge_in_trigger,
                threshold_db=float(cfg.get("barge_in_threshold_db", 6.0)),
                min_ms=int(cfg.get("barge_in_min_ms", 150)),
                reverb_decay=float(cfg.get("barge_in_reverb_decay", 0.87)),
                debug_dump_dir=str(cfg.get("barge_in_debug_dir", "")),
            )
            from brain_client.audio.echo_model import load_predictor

            predictor = load_predictor(str(cfg.get("barge_in_model_path", "")), self.logger)
            if predictor is not None:
                self._barge_in.set_echo_predictor(predictor)
            self.logger.info("🙋 Barge-in detection enabled")
        except Exception as e:
            self.logger.error(f"❌ Barge-in detector init failed (continuing without): {e}")

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
            "input_audio_buffer.speech_started": lambda e: self.logger.info("🎤 Speech detected"),
            "input_audio_buffer.speech_stopped": lambda e: self.logger.info("🔇 Speech stopped"),
            "conversation.item.input_audio_transcription.completed": lambda e: (
                self._on_transcript(e.get("transcript", "")) if e.get("transcript") and self.is_active() else None
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
        transcribe_model = self.proxy.config.get("openai_transcribe_model", "gpt-4o-mini-transcribe")
        vad_threshold = 0.3  # Lower = more sensitive to speech

        self.logger.info("📤 WebSocket opened, sending session.update...")
        session_update = {
            "type": "session.update",
            "session": {
                "input_audio_format": "pcm16",
                "input_audio_transcription": {"model": transcribe_model, "language": "en"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": float(vad_threshold),
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 700,
                    "create_response": False,
                },
                "instructions": "Transcribe user audio only in English; do not reply.",
            },
        }
        self.logger.info(f"📤 Session config: model={transcribe_model}, vad_threshold={vad_threshold}")
        self.client.send_json(session_update)
        self.logger.info("📤 session.update sent")

    def _on_openai_error(self, error):
        """Handle WebSocket error event."""
        self.logger.error(f"[ws error] {error}")

    def _on_openai_close(self):
        """Handle WebSocket close event - trigger reconnection."""
        self.logger.warning("WebSocket closed")
        self._is_connected = False

        # Don't reconnect if we're shutting down
        if self._stop_evt.is_set():
            return

        # Start reconnection in background thread
        self._schedule_reconnect()

    def _connect_via_proxy(self):
        """Connect to OpenAI Realtime API via proxy."""
        self.logger.info("🔗 Connecting to OpenAI via proxy...")

        # Get config from proxy (injected by InputManager)
        model = self.proxy.config.get("openai_realtime_model", "gpt-4o-realtime-preview")

        # Use proxy's OpenAI adapter
        self.client = self.proxy.openai.realtime.connect_sync(
            model=model,
            on_message=self._on_openai_message,
            on_open=self._on_openai_open,
            on_error=self._on_openai_error,
            on_close=self._on_openai_close,
        )
        self.client.start()

        # Start audio streaming thread
        self._start_audio_thread()

        self._is_connected = True
        self._reconnect_delay = 1  # Reset delay on successful connection
        self.logger.info(f"✅ Connected to OpenAI Realtime (model: {model})")

    def _schedule_reconnect(self):
        """Schedule a reconnection attempt in a background thread."""
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            return  # Already reconnecting

        def reconnect_loop():
            while not self._stop_evt.is_set() and not self._is_connected:
                self.logger.info(f"🔄 Reconnecting to OpenAI in {self._reconnect_delay}s...")

                # Wait before reconnecting (interruptible)
                if self._stop_evt.wait(timeout=self._reconnect_delay):
                    break  # Stop event was set

                try:
                    # Stop old client if exists. The audio thread is left
                    # running: it feeds barge-in detection during TTS and must
                    # not share fate with the WebSocket (it skips OpenAI sends
                    # while disconnected).
                    if self.client:
                        try:
                            self.client.stop()
                        except:  # noqa: E722
                            pass
                        self.client = None

                    # Reconnect
                    self._connect_via_proxy()

                    if self._is_connected:
                        self.logger.info("✅ Reconnection successful!")
                        break

                except Exception as e:
                    self.logger.error(f"❌ Reconnection failed: {e}")
                    # Exponential backoff
                    self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

        self._reconnect_thread = threading.Thread(target=reconnect_loop, daemon=True)
        self._reconnect_thread.start()

    def _start_audio_thread(self):
        """Start the audio consumer thread (one per device open, survives reconnects)."""
        if self._audio_thread and self._audio_thread.is_alive():
            return
        self._stop_evt.clear()

        def audio_loop():
            client = self.client
            if client is not None and not client.wait_until_connected(timeout=10):
                self.logger.error("WebSocket didn't connect in time (audio loop continues for barge-in)")

            self.logger.info("🎧 Audio streaming thread started")

            chunks_sent = 0
            empty_count = 0
            ducking_logged = False
            while not self._stop_evt.is_set():
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

                    # While ducking (robot speaking), run barge-in detection on
                    # the chunks instead of sending them to STT.
                    if self._is_robot_talking:
                        if not ducking_logged:
                            self.logger.info("🔇 Ducking active - running barge-in detection")
                        ducking_logged = True
                        if self._barge_in is not None:
                            try:
                                self._barge_in.feed_mic(chunk)
                            except Exception as e:
                                self.logger.error(f"barge-in feed_mic failed: {e}")
                        continue
                    ducking_logged = False

                    # reverb-tail hold-off after the robot stops speaking
                    if time.monotonic() < self._unduck_until:
                        continue

                    # Replay mic audio captured right after a barge-in trigger
                    # (only populated when barge_in_flush_tail is enabled).
                    if self._tail_to_send:
                        tail, self._tail_to_send = self._tail_to_send, []
                        for t in tail:
                            self.client.send_json(
                                {"type": "input_audio_buffer.append", "audio": base64.b64encode(t).decode("ascii")}
                            )

                    payload = {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(chunk).decode("ascii"),
                    }
                    self.client.send_json(payload)
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
            self.logger.info(f"🎙️ Selected audio device: {preferred_device['name']} ({preferred_device['id']})")

        return preferred_device["id"] if preferred_device else None

    def _on_transcript(self, text: str):
        """Called when transcript is ready."""
        if not text:
            return
        if self._is_self_echo(text):
            self.logger.info(f"🔁 Dropped self-echo transcript: {text[:80]}")
            return
        self.logger.info(f"🎤 Transcript: {text}")

        self.send_data(text, data_type="chat_in")

    def _is_self_echo(self, text: str) -> bool:
        """True if the transcript is mostly the robot's own recent speech.

        Reverb + STT hallucination can turn a leaked TTS tail into a full
        plausible sentence; since the exact TTS text is known, a transcript
        whose tokens mostly appear in a recent utterance is discarded.
        """
        tokens = _norm_tokens(text)
        if not tokens:
            return False
        now = time.monotonic()
        for stamp, tts_tokens in self._recent_tts:
            if now - stamp > 30.0:
                continue
            if len(tokens & tts_tokens) / len(tokens) >= 0.7:
                return True
        return False


def _norm_tokens(text: str) -> set:
    return set(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


# ========== Audio Streaming Helpers ==========


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
                while not self._stop.is_set():
                    buf = self._proc.stdout.read(frame_bytes)
                    if not buf:
                        # Check if process died
                        if self._proc.poll() is not None:
                            stderr = self._proc.stderr.read().decode() if self._proc.stderr else ""
                            self.logger.error(f"❌ arecord died with code {self._proc.returncode}: {stderr}")
                            break
                        time.sleep(0.01)
                        continue
                    chunks_read += 1
                    if chunks_read == 1:
                        self.logger.info(f"🎙️ First audio chunk received ({len(buf)} bytes)")
                    try:
                        self.queue.put_nowait(buf)
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
