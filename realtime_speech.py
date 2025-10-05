#!/usr/bin/env python3
"""
Real-time Speech Recognition for Maurice Robot
Simple, working solution using arecord + Google Speech Recognition
"""

import subprocess
import threading
import time
import os
import tempfile
import signal
import sys
from typing import Optional, Callable

try:
    import speech_recognition as sr
    HAS_SPEECH_RECOGNITION = True
except ImportError:
    HAS_SPEECH_RECOGNITION = False


class RealTimeSpeechRecognizer:
    """Real-time speech recognition using continuous recording and Google Speech API."""
    
    def __init__(self, 
                 chunk_duration: float = 2.0,
                 silence_threshold: int = 3,
                 mic_device: str = "hw:1,0",
                 transcribe_chunks: bool = True):
        self.chunk_duration = chunk_duration
        self.silence_threshold = silence_threshold  # Silent chunks before stopping session
        self.mic_device = mic_device
        self.transcribe_chunks = transcribe_chunks  # Transcribe each chunk immediately
        self.running = False
        self.speech_callback: Optional[Callable[[str], None]] = None
        
        # Speech detection state
        self.recording_session = False
        self.session_files = []
        self.silent_chunks = 0
        self.session_transcript = []  # Accumulate transcripts from chunks
        
        if not HAS_SPEECH_RECOGNITION:
            print("❌ speech_recognition not available. Install with: pip install speechrecognition")
    
    def is_available(self) -> bool:
        """Check if speech recognition is available."""
        return HAS_SPEECH_RECOGNITION
    
    def start_listening(self, speech_callback: Callable[[str], None]):
        """Start continuous speech recognition."""
        if not self.is_available():
            print("❌ Speech recognition not available")
            return False
        
        self.speech_callback = speech_callback
        self.running = True
        
        print("🎤 Starting real-time speech recognition...")
        print(f"🔧 Chunk duration: {self.chunk_duration}s")
        print(f"🔇 Silence threshold: {self.silence_threshold} chunks")
        print(f"⚡ Live transcription: {'ON' if self.transcribe_chunks else 'OFF'}")
        print("🗣️  Start speaking...")
        print("🛑 Press Ctrl+C to stop")
        
        try:
            while self.running:
                self._process_audio_chunk()
                time.sleep(0.1)  # Small delay between chunks
                
        except KeyboardInterrupt:
            print("\n⚠️ Speech recognition stopped by user")
        except Exception as e:
            print(f"❌ Speech recognition error: {e}")
        finally:
            self.stop_listening()
        
        return True
    
    def stop_listening(self):
        """Stop speech recognition."""
        self.running = False
        
        # Process any remaining session
        if self.session_files:
            self._finalize_speech_session()
        
        print("🔇 Speech recognition stopped")
    
    def _process_audio_chunk(self):
        """Record and process one audio chunk."""
        chunk_file = f"/tmp/speech_chunk_{int(time.time())}.wav"
        
        # Record chunk
        if self._record_chunk(chunk_file):
            # Check if chunk contains speech
            has_speech = self._detect_speech_in_chunk(chunk_file)
            
            if has_speech:
                if not self.recording_session:
                    print("🔴 Speech detected - Starting session")
                    self.recording_session = True
                    self.session_files = []
                    self.session_transcript = []
                
                self.session_files.append(chunk_file)
                self.silent_chunks = 0
                
                # Transcribe this chunk immediately if enabled
                if self.transcribe_chunks:
                    self._transcribe_chunk_immediately(chunk_file)
                else:
                    print("🎤 Recording...")
                
            else:
                # Handle silence
                if self.recording_session:
                    self.silent_chunks += 1
                    print(f"🔇 Silence {self.silent_chunks}/{self.silence_threshold}")
                    
                    if self.silent_chunks >= self.silence_threshold:
                        print("🛑 Speech session ended")
                        self._finalize_speech_session()
                        self.recording_session = False
                        self.session_files = []
                        self.session_transcript = []
                        self.silent_chunks = 0
                else:
                    # Remove silent chunks when not in session
                    if os.path.exists(chunk_file):
                        os.unlink(chunk_file)
    
    def _record_chunk(self, filename: str) -> bool:
        """Record a single audio chunk."""
        try:
            cmd = [
                "arecord",
                "-D", self.mic_device,
                "-f", "S16_LE",
                "-r", "48000",  # Native mic rate
                "-c", "2",      # Native mic channels
                "-t", "wav",
                "-d", str(int(self.chunk_duration)),
                filename
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.returncode == 0
            
        except Exception as e:
            print(f"❌ Recording failed: {e}")
            return False
    
    def _detect_speech_in_chunk(self, chunk_file: str) -> bool:
        """Simple speech detection based on file size."""
        try:
            file_size = os.path.getsize(chunk_file)
            # For 3 seconds at 48kHz stereo 16-bit: expect ~576KB
            # Threshold for speech detection
            min_size = int(self.chunk_duration * 48000 * 2 * 2 * 0.3)  # 30% of max size
            return file_size > min_size
        except:
            return False
    
    def _transcribe_chunk_immediately(self, chunk_file: str):
        """Transcribe a single chunk immediately for live feedback."""
        # Convert chunk to speech recognition format
        converted_file = f"/tmp/chunk_converted_{int(time.time())}.wav"
        self._convert_for_speech_recognition(chunk_file, converted_file)
        
        # Transcribe
        text = self._transcribe_audio(converted_file)
        
        if text and text.strip():
            # Clean up common partial words and artifacts
            text = self._clean_partial_text(text)
            if text:
                print(f"💬 Live: '{text}'")
                self.session_transcript.append(text)
        else:
            print("🎤 Recording...")
        
        # Cleanup converted file
        if os.path.exists(converted_file):
            os.unlink(converted_file)
    
    def _clean_partial_text(self, text: str) -> str:
        """Clean up partial text from live transcription."""
        # Remove very short fragments that are likely noise
        if len(text.strip()) < 2:
            return ""
        
        # Remove common speech recognition artifacts
        text = text.strip()
        artifacts = ["hmm", "uh", "um", "ah", "eh"]
        if text.lower() in artifacts:
            return ""
        
        return text
    
    def _finalize_speech_session(self):
        """Finalize a speech session and provide complete transcript."""
        if not self.session_files:
            return
        
        if self.transcribe_chunks and self.session_transcript:
            # Combine all partial transcripts
            complete_text = " ".join(self.session_transcript)
            print(f"🎯 COMPLETE: '{complete_text}'")
            
            if self.speech_callback:
                self.speech_callback(complete_text)
        else:
            # Fallback to processing the whole session
            self._process_speech_session()
        
        # Cleanup all session files
        for f in self.session_files:
            if os.path.exists(f):
                os.unlink(f)
    
    def _process_speech_session(self):
        """Process a complete speech session."""
        if not self.session_files:
            return
        
        print(f"📝 Processing speech session ({len(self.session_files)} chunks)")
        
        # Combine audio files
        combined_file = f"/tmp/speech_session_{int(time.time())}.wav"
        self._combine_audio_files(self.session_files, combined_file)
        
        # Convert to speech recognition format
        converted_file = f"/tmp/speech_converted_{int(time.time())}.wav"
        self._convert_for_speech_recognition(combined_file, converted_file)
        
        # Transcribe
        text = self._transcribe_audio(converted_file)
        
        if text and text.strip():
            print(f"💬 Transcribed: '{text}'")
            if self.speech_callback:
                self.speech_callback(text)
        else:
            print("🤷 No speech detected in session")
        
        # Cleanup
        for f in self.session_files + [combined_file, converted_file]:
            if os.path.exists(f):
                os.unlink(f)
    
    def _combine_audio_files(self, files: list, output_file: str):
        """Combine multiple audio files."""
        try:
            cmd = ["sox"] + files + [output_file]
            subprocess.run(cmd, check=True, capture_output=True)
        except:
            # Fallback: just use the first file
            if files:
                subprocess.run(["cp", files[0], output_file])
    
    def _convert_for_speech_recognition(self, input_file: str, output_file: str):
        """Convert audio to format suitable for speech recognition."""
        try:
            cmd = [
                "sox", input_file, output_file,
                "rate", "16000",    # 16kHz for better compatibility
                "channels", "1",    # Mono
                "gain", "-n"        # Normalize
            ]
            subprocess.run(cmd, check=True, capture_output=True)
        except:
            # Fallback: copy original
            subprocess.run(["cp", input_file, output_file])
    
    def _transcribe_audio(self, audio_file: str) -> Optional[str]:
        """Transcribe audio using Google Speech Recognition."""
        try:
            recognizer = sr.Recognizer()
            
            with sr.AudioFile(audio_file) as source:
                # Adjust for ambient noise
                recognizer.adjust_for_ambient_noise(source, duration=0.5)
                audio = recognizer.record(source)
            
            # Recognize speech
            text = recognizer.recognize_google(audio)
            return text
            
        except sr.UnknownValueError:
            return None
        except sr.RequestError as e:
            print(f"❌ Speech API error: {e}")
            return None
        except Exception as e:
            print(f"❌ Transcription error: {e}")
            return None


def speech_callback(text: str):
    """Handle recognized speech."""
    print(f"🎯 RECOGNIZED: '{text}'")
    print("─" * 50)


def test_recognition(live_transcription: bool = True):
    """Test speech recognition."""
    recognizer = RealTimeSpeechRecognizer(
        chunk_duration=2.0,              # 2-second chunks
        silence_threshold=3,             # 3 silent chunks = stop
        transcribe_chunks=live_transcription  # Enable live transcription
    )
    
    if recognizer.is_available():
        recognizer.start_listening(speech_callback)
    else:
        print("❌ Speech recognition not available")


def main():
    """Main function."""
    print("🤖 Maurice Robot - Real-time Speech Recognition")
    print("=" * 55)
    
    if not HAS_SPEECH_RECOGNITION:
        print("❌ speech_recognition library not found")
        print("💡 Install with: pip install speechrecognition")
        return
    
    print("🎤 This will continuously listen for speech and transcribe it")
    print("🔧 Uses Google Speech Recognition (free, requires internet)")
    print()
    print("Choose mode:")
    print("1. Live transcription (transcribe as you speak)")
    print("2. End-of-speech transcription (transcribe when you finish)")
    print()
    
    try:
        choice = input("Enter choice (1-2, default=1): ").strip()
        live_mode = choice != "2"
        
        print(f"\n🚀 Starting {'LIVE' if live_mode else 'END-OF-SPEECH'} transcription mode...")
        input("Press Enter to start listening...")
        test_recognition(live_mode)
    except KeyboardInterrupt:
        print("\n⚠️ Cancelled by user")


if __name__ == "__main__":
    # Handle Ctrl+C gracefully
    def signal_handler(sig, frame):
        print('\n⚠️ Interrupted by user')
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    main()
