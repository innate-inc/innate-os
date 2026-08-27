#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Text-to-Speech handler using Cartesia API.
Generates speech audio and plays it through the robot's audio system.
"""

import base64
import io
import queue
import struct
import subprocess
import threading
import time
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from brain_client.common.logging import UniversalLogger
from innate_proxy import ProxyClient
from innate_proxy.adapters.cartesia import ProxyCartesiaClient


@dataclass(frozen=True)
class _Utterance:
    """One queued clip, with the metadata the flush policy needs."""

    text: str
    voice_config: dict[str, Any] | None
    on_start: Callable[[], None] | None
    on_done: Callable[[bool], None] | None
    reply_id: str | None  # sentences of one streamed reply share an id
    protected: bool  # never flushed (environment speech: not our backlog)


def _survives_flush(item: _Utterance, playing_reply_id: str | None) -> bool:
    """A reply the user is hearing holds the floor: its remaining sentences
    survive a newer reply's flush. Everything else queued is stale backlog."""
    if item.protected:
        return True
    return item.reply_id is not None and item.reply_id == playing_reply_id


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

        # Initialize Cartesia client
        self._init_client()

        # Async speech is played in order by one worker so back-to-back calls
        # aren't dropped; bounded so a runaway say() loop can't build a backlog.
        self._speech_queue: deque[_Utterance | None] = deque()
        self._speech_queue_maxlen = 16
        # Guards the queue and the floor together: popping an utterance and
        # taking the floor for its reply must be one step, or a flush racing
        # the pop judges siblings against the previous reply's id.
        self._speech_cv = threading.Condition()
        self._playing_reply_id: str | None = None
        self._closing = threading.Event()
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
        on_start: Callable[[], None] | None = None,
    ) -> bool:
        """
        Convert text to speech and play it.

        Args:
            text: Text to speak
            voice_config: Optional voice configuration override
            on_start: Called once the first audio reaches the speaker

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
                success = self._synthesize_to_topic(text, voice, t_start, on_start)
            else:
                success = self._synthesize_to_aplay(text, voice, t_start, on_start)
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
        on_start: Callable[[], None] | None = None,
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
                q.put(chunk)
                if t_first_chunk is None:
                    t_first_chunk = time.perf_counter()
                    self.logger.info(f"⏱️ TTS first byte in {(t_first_chunk - t_api) * 1000:.0f}ms")
                    if on_start is not None:
                        on_start()

            t_stream_done = time.perf_counter()

            # Deliberately NOT mirrored to /tts/audio: the robot's own speaker is
            # the voice. A webapp playing the clip too doubles the speech for
            # anyone near the robot (and echoes through the mic for a remote
            # operator). /tts/audio is the sim-only path, where there is no
            # speaker (_synthesize_to_topic).

            # Signal writer to close stdin and wait for aplay to finish
            q.put(None)
            writer.join()
            player.wait()
            t_play_done = time.perf_counter()

            if player.returncode == 0:
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
            self.logger.error(f"❌ aplay failed (rc={player.returncode}): {stderr}")
            return False
        except Exception as e:
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
        on_start: Callable[[], None] | None = None,
    ) -> bool:
        """Synthesize the full clip and publish it (base64 WAV) on /tts/audio.

        The sim container has no audio device, so the webapp is the speaker. We
        collect the whole clip (utterances are short) and publish it once.
        """
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
            return False

        wav = _finalize_wav(bytes(buf))
        self._publish_audio(wav)
        if on_start is not None:
            on_start()
        # Publishing the full clip is the beginning of playback, not the end.
        # Keep /tts/is_playing true (and the serialized worker occupied) for
        # the clip's actual duration so completion callbacks match what the
        # operator hears in the browser.
        duration_s = _wav_duration_s(wav)
        if duration_s > 0:
            self._closing.wait(duration_s)  # close() must not ride out a whole clip
        self.logger.info(
            f"✅ TTS streamed to browser ({len(text)} chars, {len(buf) / 1024:.0f}KB, "
            f"total={(time.perf_counter() - t_start) * 1000:.0f}ms)"
        )
        return True

    def _publish_audio(self, wav: bytes) -> None:
        """Publish one already-finalized clip on /tts/audio as base64 WAV."""
        if self.tts_audio_pub is None or not wav:
            return
        from std_msgs.msg import String

        payload = base64.b64encode(wav).decode("ascii")
        self.tts_audio_pub.publish(String(data=payload))

    def speak_text_async(
        self,
        text: str,
        voice_config: dict[str, Any] | None = None,
        replace_pending: bool = False,
        on_start: Callable[[], None] | None = None,
        on_done: Callable[[bool], None] | None = None,
        reply_id: str | None = None,
        protected: bool = False,
    ) -> bool:
        """
        Queue text to be spoken. Utterances play in order, one at a time;
        nothing is dropped unless the queue is full. Returns immediately.

        Args:
            text: Text to speak
            voice_config: Optional voice configuration override
            replace_pending: Drop stale not-yet-played utterances first (a
                queued backlog is what makes the robot talk over the
                conversation). The reply being spoken keeps the floor: its
                remaining sentences survive, see _survives_flush.
            on_start: Optional callback invoked once, when playback begins.
            on_done: Optional callback invoked once, after playback (and one
                retry on failure) has finished.
            reply_id: Groups the sentences of one streamed reply.
            protected: Exempt from replace_pending flushes.
        """
        if not self.is_available():
            self.logger.debug("🔇 TTS not available, skipping async speech")
            return False
        dropped_callbacks = []
        with self._speech_cv:
            if replace_pending:
                kept = []
                for stale in self._speech_queue:
                    if stale is None or _survives_flush(stale, self._playing_reply_id):
                        kept.append(stale)  # keep close()'s stop sentinel too
                        continue
                    self.logger.info(f"🔇 Dropping superseded speech: '{stale.text[:60]}'")
                    if stale.on_done is not None:
                        dropped_callbacks.append(stale.on_done)
                self._speech_queue.clear()
                self._speech_queue.extend(kept)
            queued = len(self._speech_queue) < self._speech_queue_maxlen
            if queued:
                self._speech_queue.append(_Utterance(text, voice_config, on_start, on_done, reply_id, protected))
                self._speech_cv.notify()
        if not queued:
            self.logger.warning(f"🔇 Speech queue full, dropping: '{text[:60]}'")
        for callback in dropped_callbacks:
            callback(False)
        return queued

    def _drop_queued_reply(self, reply_id: str | None) -> None:
        """Discard sibling sentences after their reply fails terminally."""
        if reply_id is None:
            return
        dropped_callbacks = []
        with self._speech_cv:
            kept = []
            for queued in self._speech_queue:
                if queued is not None and queued.reply_id == reply_id:
                    self.logger.info(f"🔇 Dropping remainder of failed reply: '{queued.text[:60]}'")
                    if queued.on_done is not None:
                        dropped_callbacks.append(queued.on_done)
                    continue
                kept.append(queued)
            self._speech_queue.clear()
            self._speech_queue.extend(kept)
        for callback in dropped_callbacks:
            callback(False)

    def _once(self, callback: Callable[[], None] | None) -> Callable[[], None] | None:
        """Guard a playback-start callback: fired at most once, never raising
        into the synthesis path that calls it."""
        if callback is None:
            return None
        fired = False

        def guarded() -> None:
            nonlocal fired
            if fired:
                return
            fired = True
            try:
                callback()
            except Exception as e:  # noqa: BLE001 -- a bad listener must not abort playback
                self.logger.error(f"TTS start callback failed: {e}")

        return guarded

    def _floor_taken_on_start(self, reply_id: str | None, announce: Callable[[], None] | None) -> Callable[[], None]:
        """Take the floor when the clip becomes audible -- the only moment that
        earns a reply the right to keep its queued siblings."""

        def taken() -> None:
            self._set_playing_reply(reply_id)
            if announce is not None:
                announce()

        return taken

    def _set_playing_reply(self, reply_id: str | None) -> None:
        with self._speech_cv:
            self._playing_reply_id = reply_id

    def _speech_loop(self):
        """Single worker: plays queued utterances in order, retrying each once."""
        while True:
            with self._speech_cv:
                while not self._speech_queue:
                    self._speech_cv.wait()
                item = self._speech_queue.popleft()
            if item is None:
                break
            # Synthesis runs before a word is audible, and a failed attempt is
            # never audible at all. A reply holds the floor over those stretches
            # only if it takes it when its audio starts -- otherwise a newer
            # reply's flush spares siblings of speech nobody has heard, and they
            # play ahead of the newer answer.
            take_floor = self._floor_taken_on_start(item.reply_id, self._once(item.on_start))
            success = self.speak_text(item.text, item.voice_config, take_floor)
            if not success:
                self._set_playing_reply(None)
                self.logger.info("🔄 Retrying TTS after 1 second...")
                time.sleep(1)
                success = self.speak_text(item.text, item.voice_config, take_floor)
            if not success:
                self._set_playing_reply(None)
                self._drop_queued_reply(item.reply_id)
            if item.on_done is not None:
                try:
                    item.on_done(success)
                except Exception as e:
                    self.logger.error(f"TTS completion callback failed: {e}")

    def close(self):
        """Clean up resources."""
        self._closing.set()  # cuts a sim playback wait short (_synthesize_to_topic)
        with self._speech_cv:
            dropped = [item.on_done for item in self._speech_queue if item is not None and item.on_done is not None]
            self._speech_queue.clear()
            self._speech_queue.append(None)
            self._speech_cv.notify()
        for callback in dropped:
            callback(False)  # a dangling environment-speech ack would wait out its 30s watchdog
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


def _wav_duration_s(data: bytes) -> float:
    """Duration of one complete WAV, or zero for an undecodable response."""
    try:
        with wave.open(io.BytesIO(data), "rb") as wav:
            rate = wav.getframerate()
            return wav.getnframes() / rate if rate > 0 else 0.0
    except (EOFError, wave.Error):
        return 0.0
