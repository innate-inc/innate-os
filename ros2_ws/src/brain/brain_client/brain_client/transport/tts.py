#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Text-to-Speech handler using Cartesia API.
Generates speech audio and plays it through the robot's audio system.
"""

import base64
import json
import queue
import struct
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from brain_client.common.logging import UniversalLogger
from brain_client.transport.playback import PlaybackEvent, PlaybackObserver
from innate_proxy import ProxyClient
from innate_proxy.adapters.cartesia import ProxyCartesiaClient

_PCM_BYTES_PER_SECOND = 44_100 * 2
_WAV_HEADER_BYTES = 44


@dataclass(frozen=True)
class QueuedSpeech:
    text: str
    voice_config: dict[str, Any] | None
    playback_observer: PlaybackObserver | None
    lead_seconds: float


@dataclass
class BrowserPlayback:
    clip_id: str
    observer: PlaybackObserver | None
    done: threading.Event = field(default_factory=threading.Event)
    success: bool = False


class TTSHandler:
    """
    Handles text-to-speech conversion using Cartesia API and audio playback via aplay.

    Requires a ProxyClient instance for accessing Cartesia services.
    Voice ID starts at proxy.config["cartesia_voice_id"] and is swappable at
    runtime through set_voice; every utterance reads the current one.
    """

    # Default voice ID (Alfred)
    DEFAULT_VOICE_ID = "9fdaae0b-f885-4813-b589-3c07cf9d5fea"

    def __init__(
        self,
        logger,
        proxy: ProxyClient,
        tts_status_pub=None,
        tts_audio_pub=None,
        simulator_mode: bool = False,
    ):
        """
        Initialize the TTS handler.

        Args:
            logger: ROS logger instance or any logger
            proxy: ProxyClient instance (required)
            tts_status_pub: Optional ROS publisher for /tts/is_playing status
            tts_audio_pub: Optional ROS publisher for /tts/audio (base64 WAV).
                Used in simulator mode where there is no audio device — the
                webapp plays the clip instead of the speaker.
            simulator_mode: When True, synthesized speech is published on
                ``tts_audio_pub`` rather than played locally via aplay.
        """
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)
        self._proxy: ProxyClient = proxy
        # Get voice ID from proxy config, fall back to default
        self.voice_id: str = proxy.config.get("cartesia_voice_id", self.DEFAULT_VOICE_ID)
        self._cartesia_client: ProxyCartesiaClient | None = None
        self.is_playing: bool = False
        self.play_lock = threading.Lock()
        self.tts_status_pub = tts_status_pub
        self.tts_audio_pub = tts_audio_pub
        self._simulator_mode = simulator_mode
        self._browser_playback: BrowserPlayback | None = None
        self._browser_playback_lock = threading.Lock()

        # Initialize Cartesia client
        self._init_client()

        # Async speech is played in order by one worker so back-to-back calls
        # aren't dropped; bounded so a runaway say() loop can't build a backlog.
        self._speech_queue: queue.Queue[QueuedSpeech | None] = queue.Queue(maxsize=16)
        threading.Thread(target=self._speech_loop, daemon=True).start()

    def _init_client(self):
        """Initialize the Cartesia client via proxy."""
        try:
            self._cartesia_client = self._proxy.cartesia
            self.logger.info(f"✅ Cartesia TTS initialized via proxy (voice: {self.voice_id})")
            # Pre-warm the TCP+TLS connection to the proxy so the first
            # TTS request doesn't pay the cold-start penalty (~1-2s).
            threading.Thread(target=self._warmup_connection, daemon=True).start()
        except Exception as e:
            self.logger.error(f"❌ Failed to initialize Cartesia client: {e}")
            self.logger.error("TTS proxy not properly initialized in BrainClientNode")
            self._cartesia_client = None

    def _warmup_connection(self):
        """Open a TCP+TLS connection to the proxy so httpx can reuse it."""
        try:
            t0 = time.perf_counter()
            client = self._proxy.get_sync_client()
            # Any request to the proxy host warms the connection pool.
            client.head(self._proxy.proxy_url)
            dt = (time.perf_counter() - t0) * 1000
            self.logger.info(f"⏱️ Proxy connection pre-warmed in {dt:.0f}ms")
        except Exception as e:
            self.logger.debug(f"Proxy warmup failed (non-fatal): {e}")

    def is_available(self) -> bool:
        """Check if TTS is available and configured."""
        return self._cartesia_client is not None

    def set_voice(self, voice_id: str) -> None:
        """Speak in a different voice from the next utterance on.

        A clip already streaming keeps the voice it started with — Cartesia picks
        the voice per request, so there is nothing to swap mid-stream.
        """
        if voice_id == self.voice_id:
            return
        self.voice_id = voice_id
        self.logger.info(f"🗣️ TTS voice changed to {voice_id}")

    def _publish_tts_status(self, status: str):
        """Publish TTS playback status to /tts/is_playing topic."""
        if self.tts_status_pub:
            try:
                from std_msgs.msg import String

                msg = String()
                msg.data = status
                self.tts_status_pub.publish(msg)
            except Exception as e:
                self.logger.debug(f"Failed to publish TTS status: {e}")

    def speak_text(
        self,
        text: str,
        voice_config: dict[str, Any] | None = None,
        *,
        playback_observer: PlaybackObserver | None = None,
        lead_seconds: float = 0.0,
    ) -> bool:
        """
        Convert text to speech and play it.

        Args:
            text: Text to speak
            voice_config: Optional voice configuration override

        Returns:
            True if speech was successfully generated and played, False otherwise
        """
        if not self.is_available():
            self.logger.debug("🔇 TTS not available, skipping speech")
            return False

        if not text or not text.strip():
            self.logger.debug("🔇 Empty text provided, skipping speech")
            return False

        # Check if we're already playing audio
        with self.play_lock:
            if self.is_playing:
                self.logger.debug("🔊 Audio already playing, skipping new speech request")
                return False
            self.is_playing = True

        # Notify that TTS is starting
        self._publish_tts_status("true")

        t_start = time.perf_counter()
        text_len = len(text)
        try:
            self.logger.info(f"🗣️ TTS start ({text_len} chars): '{text[:60]}{'...' if text_len > 60 else ''}'")

            voice = voice_config or {
                "mode": "id",
                "id": self.voice_id,
            }

            if self._simulator_mode and self.tts_audio_pub is not None:
                success = self._synthesize_to_topic(text, voice, t_start, playback_observer, lead_seconds)
            else:
                success = self._synthesize_to_aplay(text, voice, t_start, playback_observer, lead_seconds)
        except Exception as e:
            self.logger.error(f"❌ TTS generation failed: {e}")
            success = False
        finally:
            with self.play_lock:
                self.is_playing = False
            self._publish_tts_status("false")

        return success

    def _stream_tts_bytes(self, text: str, voice: dict[str, Any]):
        """Yield raw WAV bytes from Cartesia as they stream in."""
        if self._cartesia_client is None:
            raise RuntimeError("Cartesia client unavailable (is_available() gates all callers)")
        return self._cartesia_client.tts.bytes_stream(
            model_id="sonic-3.5",
            transcript=text,
            voice=voice,
            output_format={
                "container": "wav",
                "encoding": "pcm_s16le",
                "sample_rate": 44100,
            },
        )

    def _synthesize_to_aplay(
        self,
        text: str,
        voice: dict[str, Any],
        t_start: float,
        playback_observer: PlaybackObserver | None,
        lead_seconds: float,
    ) -> bool:
        """Stream speech straight into aplay (real robot's speaker)."""
        text_len = len(text)
        # Volume is managed system-wide by app.cpp via
        # amixer sset Master, so aplay just uses the default.
        player = subprocess.Popen(
            ["aplay", "-q"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        # Queue + writer thread decouples the network download from
        # blocking pipe writes, exactly like the demo.
        q: queue.Queue[bytes | None] = queue.Queue()

        def _writer() -> None:
            assert player.stdin is not None
            while True:
                chunk = q.get()
                if chunk is None:
                    break
                try:
                    player.stdin.write(chunk)
                except BrokenPipeError:
                    break
            try:
                player.stdin.close()
            except Exception:
                pass

        writer = threading.Thread(target=_writer, daemon=True)
        writer.start()
        cue_timer: threading.Timer | None = None

        try:
            total_bytes = 0
            chunk_count = 0
            t_first_chunk = None

            t_api = time.perf_counter()
            for chunk in self._stream_tts_bytes(text, voice):
                if not chunk:
                    continue
                chunk_count += 1
                total_bytes += len(chunk)
                if t_first_chunk is None:
                    t_first_chunk = time.perf_counter()
                    self.logger.info(f"⏱️ TTS first byte in {(t_first_chunk - t_api) * 1000:.0f}ms")
                    self._emit_playback(playback_observer, PlaybackEvent.STARTED)
                q.put(chunk)

            if total_bytes == 0:
                raise RuntimeError("TTS produced no audio")
            t_stream_done = time.perf_counter()

            # Deliberately NOT mirrored to /tts/audio: the robot's own speaker is
            # the voice. A webapp playing the clip too doubles the speech for
            # anyone near the robot (and echoes through the mic for a remote
            # operator). /tts/audio is the sim-only path, where there is no
            # speaker (_synthesize_to_topic).

            cue_timer = self._schedule_near_end(
                playback_observer,
                duration_seconds=_pcm_duration_seconds(total_bytes),
                playback_started=t_first_chunk,
                lead_seconds=lead_seconds,
            )
            q.put(None)
            writer.join()
            player.wait()
            t_play_done = time.perf_counter()

            if player.returncode == 0:
                if cue_timer is not None:
                    cue_pending = cue_timer.is_alive()
                    cue_timer.cancel()
                    if cue_pending:
                        self._emit_playback(playback_observer, PlaybackEvent.NEAR_END)
                self._emit_playback(playback_observer, PlaybackEvent.ENDED)
                ttfb_ms = (t_first_chunk - t_api) * 1000 if t_first_chunk else 0
                self.logger.info(
                    f"✅ TTS done ({text_len} chars): "
                    f"TTFB={ttfb_ms:.0f}ms "
                    f"stream={(t_stream_done - t_api) * 1000:.0f}ms "
                    f"total={(t_play_done - t_start) * 1000:.0f}ms "
                    f"({total_bytes / 1024:.0f}KB, {chunk_count} chunks)"
                )
                return True
            stderr = player.stderr.read().decode(errors="replace").strip() if player.stderr else ""
            if cue_timer is not None:
                cue_timer.cancel()
            self._emit_playback(playback_observer, PlaybackEvent.ABORTED)
            self.logger.error(f"❌ aplay failed (rc={player.returncode}): {stderr}")
            return False
        except Exception as e:
            if cue_timer is not None:
                cue_timer.cancel()
            self._emit_playback(playback_observer, PlaybackEvent.ABORTED)
            self.logger.error(f"❌ Streaming TTS failed: {e}")
            q.put(None)
            writer.join(timeout=2)
            try:
                player.kill()
            except Exception:
                pass
            return False

    def _synthesize_to_topic(
        self,
        text: str,
        voice: dict[str, Any],
        t_start: float,
        playback_observer: PlaybackObserver | None,
        lead_seconds: float,
    ) -> bool:
        """Synthesize one browser clip and wait for its actual playback result."""
        t_api = time.perf_counter()
        buf = bytearray()
        t_first_chunk = None
        for chunk in self._stream_tts_bytes(text, voice):
            if not chunk:
                continue
            if t_first_chunk is None:
                t_first_chunk = time.perf_counter()
                self.logger.info(f"⏱️ TTS first byte in {(t_first_chunk - t_api) * 1000:.0f}ms")
            buf.extend(chunk)

        if not buf:
            self.logger.error("❌ TTS produced no audio")
            self._emit_playback(playback_observer, PlaybackEvent.ABORTED)
            return False

        duration_seconds = _pcm_duration_seconds(len(buf))
        playback = BrowserPlayback(uuid.uuid4().hex, playback_observer)
        with self._browser_playback_lock:
            self._browser_playback = playback
        self._publish_audio(bytes(buf), playback.clip_id, lead_seconds)
        timeout = max(15.0, duration_seconds + 10.0)
        if not playback.done.wait(timeout):
            self.logger.error(f"Browser did not finish TTS clip {playback.clip_id} within {timeout:.1f}s")
            playback.done.set()
            self._emit_playback(playback.observer, PlaybackEvent.ABORTED)
        with self._browser_playback_lock:
            if self._browser_playback is playback:
                self._browser_playback = None
        self.logger.info(
            f"{'✅' if playback.success else '❌'} TTS browser playback "
            f"({len(text)} chars, {len(buf) / 1024:.0f}KB, "
            f"total={(time.perf_counter() - t_start) * 1000:.0f}ms)"
        )
        return playback.success

    def _publish_audio(self, wav: bytes, clip_id: str, lead_seconds: float) -> None:
        """Publish one identified browser playback request."""
        if self.tts_audio_pub is None or not wav:
            return
        from std_msgs.msg import String

        payload = {
            "id": clip_id,
            "audio": base64.b64encode(_finalize_wav(wav)).decode("ascii"),
            "near_end_lead_seconds": max(0.0, lead_seconds),
        }
        self.tts_audio_pub.publish(String(data=json.dumps(payload, separators=(",", ":"))))

    def on_browser_playback(self, payload: str) -> None:
        """Forward an elected simulator speaker's playback event."""
        try:
            data = json.loads(payload)
            clip_id = data["id"]
            event = PlaybackEvent(data["event"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            self.logger.warning("Ignoring invalid /tts/playback event")
            return
        with self._browser_playback_lock:
            playback = self._browser_playback
        if playback is None or clip_id != playback.clip_id or playback.done.is_set():
            return
        if event == PlaybackEvent.ENDED:
            playback.success = True
            playback.done.set()
        elif event == PlaybackEvent.ABORTED:
            playback.done.set()
        self._emit_playback(playback.observer, event)

    def speak_text_async(
        self,
        text: str,
        voice_config: dict[str, Any] | None = None,
        replace_pending: bool = False,
        *,
        playback_observer: PlaybackObserver | None = None,
        lead_seconds: float = 0.0,
    ) -> None:
        """
        Queue text to be spoken. Utterances play in order, one at a time;
        nothing is dropped unless the queue is full. Returns immediately.

        Args:
            text: Text to speak
            voice_config: Optional voice configuration override
            replace_pending: Drop any not-yet-played utterances first. Used for
                conversational replies, where a newer reply supersedes queued
                older ones (playing a backlog is what makes the robot talk
                over the conversation). The currently playing clip finishes.
        """
        if not self.is_available():
            self.logger.debug("🔇 TTS not available, skipping async speech")
            return
        try:
            if replace_pending:
                while True:
                    try:
                        stale = self._speech_queue.get_nowait()
                        if stale is not None:
                            self.logger.info(f"🔇 Dropping superseded speech: '{stale.text[:60]}'")
                            self._emit_playback(stale.playback_observer, PlaybackEvent.ABORTED)
                    except queue.Empty:
                        break
            self._speech_queue.put_nowait(
                QueuedSpeech(
                    text=text,
                    voice_config=voice_config,
                    playback_observer=_unique_observer(playback_observer),
                    lead_seconds=lead_seconds,
                )
            )
        except queue.Full:
            self.logger.warning(f"🔇 Speech queue full, dropping: '{text[:60]}'")
            self._emit_playback(playback_observer, PlaybackEvent.ABORTED)

    def _speech_loop(self):
        """Single worker: plays queued utterances in order, retrying each once."""
        while True:
            item = self._speech_queue.get()
            if item is None:
                break
            if not self.speak_text(
                item.text,
                item.voice_config,
                playback_observer=item.playback_observer,
                lead_seconds=item.lead_seconds,
            ):
                if item.playback_observer is not None:
                    continue
                self.logger.info("🔄 Retrying TTS after 1 second...")
                time.sleep(1)
                self.speak_text(
                    item.text,
                    item.voice_config,
                    playback_observer=None,
                )

    def _schedule_near_end(
        self,
        observer: PlaybackObserver | None,
        *,
        duration_seconds: float,
        playback_started: float | None,
        lead_seconds: float,
    ) -> threading.Timer | None:
        if observer is None:
            return None
        elapsed = time.perf_counter() - playback_started if playback_started is not None else 0.0
        delay = max(0.0, duration_seconds - elapsed - max(0.0, lead_seconds))
        timer = threading.Timer(delay, self._emit_playback, args=(observer, PlaybackEvent.NEAR_END))
        timer.daemon = True
        timer.start()
        return timer

    def _emit_playback(self, observer: PlaybackObserver | None, event: PlaybackEvent) -> None:
        if observer is None:
            return
        try:
            observer(event)
        except Exception as error:  # noqa: BLE001 — playback reporting must not break speech
            self.logger.error(f"Speech playback observer failed: {error}")

    def close(self):
        """Clean up resources."""
        # drop any queued backlog, then hand the worker its stop sentinel;
        # a blocking put() could hang shutdown if the queue is full
        try:
            while True:
                queued = self._speech_queue.get_nowait()
                if queued is not None:
                    self._emit_playback(queued.playback_observer, PlaybackEvent.ABORTED)
        except queue.Empty:
            pass
        with self._browser_playback_lock:
            playback, self._browser_playback = self._browser_playback, None
        if playback is not None:
            playback.done.set()
            self._emit_playback(playback.observer, PlaybackEvent.ABORTED)
        try:
            self._speech_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._cartesia_client:
            self.logger.info("🔇 TTS handler closed")
            # Cartesia client doesn't need explicit cleanup in sync mode
            self._cartesia_client = None


def _finalize_wav(data: bytes) -> bytes:
    """Patch the RIFF/data chunk sizes of a fully-collected WAV.

    Cartesia streams WAV with placeholder length fields (the size isn't known
    until the stream ends). aplay tolerates that, but browser decoders are
    stricter, so once we have the whole clip we write the real lengths in.
    """
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return data
    out = bytearray(data)
    data_idx = out.find(b"data", 12)
    if data_idx == -1 or data_idx + 8 > len(out):
        return bytes(out)
    struct.pack_into("<I", out, 4, len(out) - 8)  # RIFF chunk size
    struct.pack_into("<I", out, data_idx + 4, len(out) - (data_idx + 8))  # data size
    return bytes(out)


def _pcm_duration_seconds(byte_count: int) -> float:
    return max(0, byte_count - _WAV_HEADER_BYTES) / _PCM_BYTES_PER_SECOND


def _unique_observer(observer: PlaybackObserver | None) -> PlaybackObserver | None:
    if observer is None:
        return None
    seen: set[PlaybackEvent] = set()
    lock = threading.Lock()

    def notify(event: PlaybackEvent) -> None:
        with lock:
            if event in seen:
                return
            seen.add(event)
        observer(event)

    return notify
