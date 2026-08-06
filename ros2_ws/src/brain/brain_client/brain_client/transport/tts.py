#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Text-to-Speech handler using Cartesia API.
Generates speech audio and plays it through the robot's audio system.
"""

import base64
import fcntl
import json
import queue
import struct
import subprocess
import threading
import time
from typing import Any

from brain_client.common.logging import UniversalLogger
from innate_proxy import ProxyClient
from innate_proxy.adapters.cartesia import ProxyCartesiaClient


class TTSHandler:
    """
    Handles text-to-speech conversion using Cartesia API and audio playback via aplay.

    Requires a ProxyClient instance for accessing Cartesia services.
    Voice ID is read from proxy.config["cartesia_voice_id"].
    """

    # Default voice ID (Alfred)
    DEFAULT_VOICE_ID = "9fdaae0b-f885-4813-b589-3c07cf9d5fea"

    def __init__(
        self,
        logger,
        proxy: ProxyClient,
        tts_status_pub=None,
        tts_audio_pub=None,
        tts_ref_pub=None,
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
            tts_ref_pub: Optional ROS publisher for /tts/ref_audio — the exact
                PCM being piped to the speaker, published as it is written so
                barge-in detection has an echo reference. Hardware path only.
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
        self.tts_ref_pub = tts_ref_pub
        self._simulator_mode = simulator_mode
        self._abort = threading.Event()
        self._player: subprocess.Popen | None = None
        self._utt_seq = 0
        self._hold_until = 0.0

        # Initialize Cartesia client
        self._init_client()

        # Async speech is played in order by one worker so back-to-back calls
        # aren't dropped; bounded so a runaway say() loop can't build a backlog.
        self._speech_queue: queue.Queue = queue.Queue(maxsize=16)
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

    def speak_text(self, text: str, voice_config: dict[str, Any] | None = None) -> bool:
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

        # A barge-in means "shut up and listen": drop utterances (typically the
        # tail of the interrupted reply) until the user's words arrive as a
        # chat_in (clear_hold) or the hold times out. True = handled, no retry.
        if time.monotonic() < self._hold_until:
            self.logger.info(f"🤫 Holding after barge-in, dropped utterance: '{text[:50]}'")
            return True

        # Check if we're already playing audio
        with self.play_lock:
            if self.is_playing:
                self.logger.debug("🔊 Audio already playing, skipping new speech request")
                return False
            self.is_playing = True
            self._abort.clear()
            self._utt_seq += 1

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
                success = self._synthesize_to_topic(text, voice, t_start)
            else:
                success = self._synthesize_to_aplay(text, voice, t_start)
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

    def _synthesize_to_aplay(self, text: str, voice: dict[str, Any], t_start: float) -> bool:
        """Stream speech straight into aplay (real robot's speaker)."""
        text_len = len(text)
        seq = self._utt_seq
        # Volume is managed system-wide by app.cpp via
        # amixer sset Master, so aplay just uses the default.
        player = subprocess.Popen(
            ["aplay", "-q"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        with self.play_lock:
            self._player = player
        # Shrink the stdin pipe so the barge-in reference tee (published as the
        # writer hands bytes to aplay) leads the speaker by a bounded ~200ms
        # instead of the default 64KB (~750ms of audio).
        try:
            fcntl.fcntl(player.stdin.fileno(), 1031, 16384)  # F_SETPIPE_SZ
        except OSError:
            pass

        # text rides along so the mic input can drop self-echo transcripts
        self._publish_ref({"e": "start", "seq": seq, "rate": 44100, "text": text})
        stripper = _WavHeaderStripper()

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
                except (BrokenPipeError, ValueError, OSError):
                    break
                # Tee the PCM as the echo reference only once the pipe has
                # accepted it, so the published stream tracks playback pace.
                pcm = stripper.feed(chunk)
                if pcm:
                    self._publish_ref({"e": "pcm", "seq": seq, "pcm": base64.b64encode(pcm).decode("ascii")})
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
                if self._abort.is_set():
                    break
                if not chunk:
                    continue
                chunk_count += 1
                total_bytes += len(chunk)
                if t_first_chunk is None:
                    t_first_chunk = time.perf_counter()
                    self.logger.info(f"⏱️ TTS first byte in {(t_first_chunk - t_api) * 1000:.0f}ms")
                q.put(chunk)

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

            if self._abort.is_set():
                self.logger.info(f"✋ TTS aborted mid-utterance ({text_len} chars)")
                return True  # handled: no retry for an intentional stop
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
        finally:
            with self.play_lock:
                self._player = None
            self._publish_ref({"e": "end", "seq": seq})

    def _publish_ref(self, event: dict[str, Any]) -> None:
        """Publish a barge-in reference event on /tts/ref_audio."""
        if self.tts_ref_pub is None:
            return
        try:
            from std_msgs.msg import String

            self.tts_ref_pub.publish(String(data=json.dumps(event)))
        except Exception as e:
            self.logger.debug(f"Failed to publish TTS ref: {e}")

    def clear_hold(self) -> None:
        """The user's words reached the brain; its next reply may speak."""
        if self._hold_until and time.monotonic() < self._hold_until:
            self.logger.info("🎤 Barge-in hold released (user input arrived)")
        self._hold_until = 0.0

    def stop_current(self, reason: str = "", hold_s: float = 5.0) -> bool:
        """Stop the utterance playing right now and drop any queued ones.

        Returns True if something was actually stopped. Safe from any thread;
        used by barge-in (the human is talking — shut up immediately). New
        utterances are held for ``hold_s`` or until clear_hold().
        """
        self._hold_until = time.monotonic() + hold_s
        dropped = 0
        try:
            while True:
                self._speech_queue.get_nowait()
                dropped += 1
        except queue.Empty:
            pass
        with self.play_lock:
            playing = self.is_playing
            player = self._player
        if not playing:
            return False
        self._abort.set()
        if player is not None and player.poll() is None:
            try:
                player.kill()  # kill, not terminate: silence NOW, don't drain
            except Exception:
                pass
        self.logger.info(f"✋ TTS stopped ({reason or 'requested'}); dropped {dropped} queued utterance(s)")
        return True

    def _synthesize_to_topic(self, text: str, voice: dict[str, Any], t_start: float) -> bool:
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

        self._publish_audio(bytes(buf))
        self.logger.info(
            f"✅ TTS streamed to browser ({len(text)} chars, {len(buf) / 1024:.0f}KB, "
            f"total={(time.perf_counter() - t_start) * 1000:.0f}ms)"
        )
        return True

    def _publish_audio(self, wav: bytes) -> None:
        """Publish a finished clip on /tts/audio as base64 WAV for clients to play."""
        if self.tts_audio_pub is None or not wav:
            return
        from std_msgs.msg import String

        payload = base64.b64encode(_finalize_wav(wav)).decode("ascii")
        self.tts_audio_pub.publish(String(data=payload))

    def speak_text_async(self, text: str, voice_config: dict[str, Any] | None = None) -> None:
        """
        Queue text to be spoken. Utterances play in order, one at a time;
        nothing is dropped unless the queue is full. Returns immediately.

        Args:
            text: Text to speak
            voice_config: Optional voice configuration override
        """
        if not self.is_available():
            self.logger.debug("🔇 TTS not available, skipping async speech")
            return
        try:
            self._speech_queue.put_nowait((text, voice_config))
        except queue.Full:
            self.logger.warning(f"🔇 Speech queue full, dropping: '{text[:60]}'")

    def _speech_loop(self):
        """Single worker: plays queued utterances in order, retrying each once."""
        while True:
            item = self._speech_queue.get()
            if item is None:
                break
            text, voice_config = item
            if not self.speak_text(text, voice_config):
                self.logger.info("🔄 Retrying TTS after 1 second...")
                time.sleep(1)
                self.speak_text(text, voice_config)

    def close(self):
        """Clean up resources."""
        # drop any queued backlog, then hand the worker its stop sentinel;
        # a blocking put() could hang shutdown if the queue is full
        try:
            while True:
                self._speech_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._speech_queue.put_nowait(None)
        except queue.Full:
            pass
        if self._cartesia_client:
            self.logger.info("🔇 TTS handler closed")
            # Cartesia client doesn't need explicit cleanup in sync mode
            self._cartesia_client = None


class _WavHeaderStripper:
    """Drops the RIFF/fmt header from a streamed WAV, yielding raw PCM."""

    def __init__(self):
        self._buf = b""
        self._pcm_started = False

    def feed(self, chunk: bytes) -> bytes:
        if self._pcm_started:
            return chunk
        self._buf += chunk
        idx = self._buf.find(b"data")
        if idx == -1 or len(self._buf) < idx + 8:
            return b""
        self._pcm_started = True
        return self._buf[idx + 8 :]


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
