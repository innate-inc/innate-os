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
from typing import Optional, Dict, Any

import rclpy
from cartesia import Cartesia
import subprocess


class TTSHandler:
    """
    Handles text-to-speech conversion using Cartesia API and audio playback via aplay.
    """

    def __init__(self, api_key: str, logger, voice_id: str = "a0e99841-438c-4a64-b679-ae501e7d6091", tts_status_pub=None):
        """
        Initialize the TTS handler.
        
        Args:
            api_key: Cartesia API key
            logger: ROS logger instance
            voice_id: Voice ID to use for speech synthesis
            tts_status_pub: Optional ROS publisher for /tts/is_playing status
        """
        self.logger = logger
        self.api_key = api_key
        self.voice_id = voice_id
        self.client = None
        self.is_playing = False
        self.play_lock = threading.Lock()
        self.tts_status_pub = tts_status_pub
        
        # Initialize client if API key is provided
        if self.api_key and self.api_key.strip():
            try:
                self.client = Cartesia(api_key=self.api_key)
                self.logger.info(f"✅ Cartesia TTS initialized with voice ID: {self.voice_id}")
            except Exception as e:
                self.logger.error(f"❌ Failed to initialize Cartesia client: {e}")
                self.client = None
        else:
            self.logger.warn("⚠️ No Cartesia API key provided - TTS disabled")

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
            
            # Generate speech using Cartesia
            response = self.client.tts.bytes(
                model_id="sonic-2",  # Latest Cartesia model
                transcript=text,
                voice=voice,
                output_format={
                    "container": "wav",
                    "encoding": "pcm_s16le",
                    "sample_rate": 44100
                },
            )
            
            # Convert generator to bytes if needed
            if hasattr(response, '__iter__') and not isinstance(response, (bytes, bytearray)):
                # Response is a generator, collect all chunks
                audio_data = b''.join(response)
            else:
                # Response is already bytes
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
        
        Returns:
            List of available voice objects
        """
        if not self.is_available():
            return []
            
        try:
            voices = list(self.client.voices.list())
            self.logger.debug(f"📋 Found {len(voices)} available voices")
            return voices
        except Exception as e:
            self.logger.error(f"❌ Failed to fetch available voices: {e}")
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
