#!/usr/bin/env python3
"""
Text-to-Speech handler using Cartesia API.
Generates speech audio and plays it through the robot's audio system.
"""

import json
import os
import queue
import threading
import time
from typing import Optional, Dict, Any

import rclpy
import subprocess

from innate_proxy import ProxyClient
from innate_proxy.adapters.cartesia import ProxyCartesiaClient
from brain_client.logging_config import UniversalLogger


class TTSHandler:
    """
    Handles text-to-speech conversion using Cartesia API and audio playback via paplay.
    
    Requires a ProxyClient instance for accessing Cartesia services.
    Voice ID is read from proxy.config["cartesia_voice_id"].
    """
    
    # Default voice ID (Katie - Friendly Fixer)
    DEFAULT_VOICE_ID = "9fdaae0b-f885-4813-b589-3c07cf9d5fea"

    def __init__(
        self,
        logger,
        proxy: ProxyClient,
        tts_status_pub=None,
    ):
        """
        Initialize the TTS handler.
        
        Args:
            logger: ROS logger instance or any logger
            proxy: ProxyClient instance (required)
            tts_status_pub: Optional ROS publisher for /tts/is_playing status
        """
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)
        self._proxy: ProxyClient = proxy
        # Get voice ID from proxy config, fall back to default
        self.voice_id: str = proxy.config.get("cartesia_voice_id", self.DEFAULT_VOICE_ID)
        self._cartesia_client: Optional[ProxyCartesiaClient] = None
        self.is_playing: bool = False
        self.play_lock = threading.Lock()
        self.tts_status_pub = tts_status_pub
        
        # Speaker keep-alive: feed silence through PulseAudio to prevent
        # the audio codec/amplifier from suspending and causing cracks/pops
        self._keepalive_process: Optional[subprocess.Popen] = None
        self._start_speaker_keepalive()

        # Initialize Cartesia client
        self._init_client()
    
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
    
    def _start_speaker_keepalive(self):
        """Start a background silence stream via PulseAudio to keep the speaker active.

        Prevents the audio codec/amplifier from powering down between utterances,
        which causes an audible crack/pop when it wakes up. Uses pacat (PulseAudio)
        rather than aplay (ALSA) so it doesn't exclusively lock the hardware device.
        """
        try:
            self._keepalive_process = subprocess.Popen(
                ["pacat", "--playback", "/dev/zero",
                 "--format=s16le", "--rate=48000", "--channels=2"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.logger.info("🔊 Speaker keep-alive started (PulseAudio silence stream)")
        except Exception as e:
            self.logger.warn(f"⚠️ Failed to start speaker keep-alive: {e}")
            self._keepalive_process = None

    def _stop_speaker_keepalive(self):
        """Stop the background silence stream."""
        if self._keepalive_process is not None:
            try:
                self._keepalive_process.kill()
                self._keepalive_process.wait()
            except Exception:
                pass
            self._keepalive_process = None

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

    def speak_text(self, text: str, voice_config: Optional[Dict[str, Any]] = None) -> bool:
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

            # Volume is managed system-wide by app.cpp via
            # pactl set-sink-volume, so paplay just uses the default.
            player = subprocess.Popen(
                ["paplay"],
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
                for chunk in self._cartesia_client.tts.bytes_stream(
                    model_id="sonic-3",
                    transcript=text,
                    voice=voice,
                    output_format={
                        "container": "wav",
                        "encoding": "pcm_s16le",
                        "sample_rate": 44100,
                    },
                ):
                    if not chunk:
                        continue
                    chunk_count += 1
                    total_bytes += len(chunk)
                    if t_first_chunk is None:
                        t_first_chunk = time.perf_counter()
                        self.logger.info(
                            f"⏱️ TTS first byte in {(t_first_chunk - t_api)*1000:.0f}ms"
                        )
                    q.put(chunk)

                t_stream_done = time.perf_counter()

                # Signal writer to close stdin and wait for paplay to finish
                q.put(None)
                writer.join()
                player.wait()
                t_play_done = time.perf_counter()

                if player.returncode == 0:
                    ttfb_ms = (t_first_chunk - t_api) * 1000 if t_first_chunk else 0
                    self.logger.info(
                        f"✅ TTS done ({text_len} chars): "
                        f"TTFB={ttfb_ms:.0f}ms "
                        f"stream={(t_stream_done - t_api)*1000:.0f}ms "
                        f"total={(t_play_done - t_start)*1000:.0f}ms "
                        f"({total_bytes/1024:.0f}KB, {chunk_count} chunks)"
                    )
                    success = True
                else:
                    stderr = player.stderr.read().decode(errors="replace").strip() if player.stderr else ""
                    self.logger.error(f"❌ paplay failed (rc={player.returncode}): {stderr}")
                    success = False
            except Exception as e:
                self.logger.error(f"❌ Streaming TTS failed: {e}")
                q.put(None)
                writer.join(timeout=2)
                try:
                    player.kill()
                except Exception:
                    pass
                success = False

        except Exception as e:
            self.logger.error(f"❌ TTS generation failed: {e}")
            success = False
        finally:
            with self.play_lock:
                self.is_playing = False
            self._publish_tts_status("false")

        return success

    def speak_text_async(self, text: str, voice_config: Optional[Dict[str, Any]] = None) -> None:
        """
        Convert text to speech and play it asynchronously in a separate thread.
        Retries once with 1 second gap on failure.
        
        Args:
            text: Text to speak
            voice_config: Optional voice configuration override
        """
        if not self.is_available():
            self.logger.debug("🔇 TTS not available, skipping async speech")
            return

        def speak_thread():
            if not self.speak_text(text, voice_config):
                self.logger.info("🔄 Retrying TTS after 1 second...")
                time.sleep(1)
                self.speak_text(text, voice_config)
            
        speech_thread = threading.Thread(target=speak_thread, daemon=True)
        speech_thread.start()

    def get_available_voices(self) -> list:
        """
        Get list of available voices from Cartesia.
        
        Note: This method is not yet implemented in the proxy client.
        Returns empty list for now.
        
        Returns:
            List of available voice objects
        """
        if not self.is_available():
            return []
            
        # TODO: Implement voice listing in proxy client if needed
        self.logger.warn("⚠️ Voice listing not yet implemented in proxy client")
        return []

    def set_voice(self, voice_id: str) -> bool:
        """
        Set the voice ID for speech synthesis.
        
        Args:
            voice_id: New voice ID to use
            
        Returns:
            True if voice was set successfully, False otherwise
        """
        if not self.is_available():
            return False
            
        try:
            # Verify the voice exists
            voices = self.get_available_voices()
            voice_ids = [voice.id for voice in voices]
            
            if voice_id in voice_ids:
                self.voice_id = voice_id
                self.logger.info(f"✅ Voice updated to: {voice_id}")
                return True
            else:
                self.logger.error(f"❌ Voice ID not found: {voice_id}")
                return False
        except Exception as e:
            self.logger.error(f"❌ Failed to set voice: {e}")
            return False

    def close(self):
        """Clean up resources."""
        self._stop_speaker_keepalive()
        if self._cartesia_client:
            self.logger.info("🔇 TTS handler closed")
            # Cartesia client doesn't need explicit cleanup in sync mode
            self._cartesia_client = None
