#!/usr/bin/env python3
"""
Text-to-Speech handler using Cartesia API.
Generates speech audio and plays it through the robot's audio system.
"""

import asyncio
import os
import tempfile
import threading
import time
from typing import Optional, Dict, Any, TYPE_CHECKING

import rclpy
import subprocess

if TYPE_CHECKING:
    from brain_client.client.proxy_client import ProxyClient


class TTSHandler:
    """
    Handles text-to-speech conversion using Cartesia API and audio playback via aplay.
    
    Can be initialized with a ProxyClient (preferred) or will create its own.
    """
    
    # Default voice ID (Katie - Friendly Fixer)
    DEFAULT_VOICE_ID = "f786b574-daa5-4673-aa0c-cbe3e8534c02"

    def __init__(
        self,
        logger=None,
        voice_id: str = None,
        tts_status_pub=None,
        proxy: "ProxyClient" = None,
    ):
        """
        Initialize the TTS handler.
        
        Args:
            logger: ROS logger instance
            voice_id: Voice ID to use for speech synthesis
            tts_status_pub: Optional ROS publisher for /tts/is_playing status
            proxy: Optional ProxyClient instance (if not provided, creates its own)
        """
        self.logger = logger
        self.voice_id = voice_id or self.DEFAULT_VOICE_ID
        self._proxy = proxy
        self._cartesia_client = None
        self.is_playing = False
        self.play_lock = threading.Lock()
        self.tts_status_pub = tts_status_pub
        
        # Initialize Cartesia client
        self._init_client()
    
    def _init_client(self):
        """Initialize the Cartesia client (via proxy or directly)."""
        try:
            if self._proxy and self._proxy.is_available():
                # Use injected proxy
                self._cartesia_client = self._proxy.cartesia
                if self.logger:
                    self.logger.info(f"✅ Cartesia TTS initialized via proxy (voice: {self.voice_id})")
            else:
                # Create standalone ProxyCartesiaClient (reads from env)
                from brain_client.client.adapters.cartesia_adapter import ProxyCartesiaClient
                self._cartesia_client = ProxyCartesiaClient()
                if self.logger:
                    self.logger.info(f"✅ Cartesia TTS initialized (voice: {self.voice_id})")
        except Exception as e:
            if self.logger:
                self.logger.error(f"❌ Failed to initialize Cartesia client: {e}")
                self.logger.error("Make sure INNATE_PROXY_URL and INNATE_SERVICE_KEY are set")
            self._cartesia_client = None
    
    @property
    def client(self):
        """Backward-compatible access to Cartesia client."""
        return self._cartesia_client
    
    @client.setter
    def client(self, value):
        """Backward-compatible setter."""
        self._cartesia_client = value

    def is_available(self) -> bool:
        """Check if TTS is available and configured."""
        return self.client is not None
    
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

        try:
            self.logger.info(f"🗣️ Generating speech for: '{text[:50]}{'...' if len(text) > 50 else ''}'")
            
            # Set up voice configuration
            voice = voice_config or {
                "mode": "id",
                "id": self.voice_id,
            }
            
            # Generate speech using Cartesia via proxy (async)
            # Create new event loop for this synchronous method
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            response = loop.run_until_complete(
                self.client.tts.bytes(
                model_id="sonic-3",  # Latest Cartesia model
                transcript=text,
                voice=voice,
                output_format={
                    "container": "wav",
                    "encoding": "pcm_s16le",
                    "sample_rate": 44100
                },
                )
            )
            
            # If response is an iterator of chunks, stream directly into aplay.
            # Otherwise, fall back to the existing temp-file approach.
            if hasattr(response, '__iter__') and not isinstance(response, (bytes, bytearray)):
                self.logger.info("🔊 Streaming TTS audio to aplay via stdin")
                player = subprocess.Popen(
                    ["aplay", "-"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=False,
                )

                try:
                    for chunk in response:
                        if not chunk:
                            continue
                        try:
                            player.stdin.write(chunk)
                            player.stdin.flush()
                        except BrokenPipeError:
                            # aplay exited early; stop streaming
                            self.logger.warn("🔊 aplay pipe closed early during streaming")
                            break

                    # Close stdin to signal end of stream and wait for aplay to finish
                    try:
                        if player.stdin:
                            player.stdin.close()
                    except Exception:
                        pass

                    player.wait()
                    if player.returncode == 0:
                        self.logger.info("✅ Speech playback completed successfully (streaming)")
                        success = True
                    else:
                        self.logger.error(f"❌ Audio playback failed (streaming), return code {player.returncode}")
                        success = False
                except Exception as stream_error:
                    self.logger.error(f"❌ Streaming TTS playback failed: {stream_error}")
                    # Ensure the process is cleaned up
                    try:
                        if player.stdin and not player.stdin.closed:
                            player.stdin.close()
                    except Exception:
                        pass
                    try:
                        player.kill()
                    except Exception:
                        pass
                    success = False
            else:
                # Response is already bytes: use the original temp-file playback path.
                audio_data = response
                
                # Save to temporary file
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
                    temp_file.write(audio_data)
                    temp_filename = temp_file.name
                
                # Play the audio using aplay
                self.logger.debug(f"🔊 Playing audio file: {temp_filename}")
                result = subprocess.run(
                    ["aplay", temp_filename],
                    capture_output=True,
                    text=True,
                    check=False
                )
                
                if result.returncode == 0:
                    self.logger.info("✅ Speech playback completed successfully")
                    success = True
                else:
                    self.logger.error(f"❌ Audio playback failed: {result.stderr}")
                    success = False
                
        except Exception as e:
            self.logger.error(f"❌ TTS generation failed: {e}")
            success = False
        finally:
            # Clean up temporary file
            try:
                if 'temp_filename' in locals():
                    os.unlink(temp_filename)
            except Exception as cleanup_error:
                self.logger.warn(f"⚠️ Failed to cleanup temp file: {cleanup_error}")
            
            # Release the lock
            with self.play_lock:
                self.is_playing = False
            
            # Notify that TTS is done
            self._publish_tts_status("false")
                
        return success

    def speak_text_async(self, text: str, voice_config: Optional[Dict[str, Any]] = None) -> None:
        """
        Convert text to speech and play it asynchronously in a separate thread.
        
        Args:
            text: Text to speak
            voice_config: Optional voice configuration override
        """
        if not self.is_available():
            self.logger.debug("🔇 TTS not available, skipping async speech")
            return

        def speak_thread():
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
        if self.client:
            self.logger.info("🔇 TTS handler closed")
            # Cartesia client doesn't need explicit cleanup in sync mode
            self.client = None
