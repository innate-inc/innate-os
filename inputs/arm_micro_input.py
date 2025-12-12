#!/usr/bin/env python3
"""
ARM Microphone Input Device (PulseAudio)

Connects to microphone hardware via PulseAudio and OpenAI's realtime API to get voice transcripts.
This is a pure Python class with NO ROS dependencies.

Uses parec (PulseAudio) instead of arecord (ALSA) for audio capture.
"""
import base64
import json
import os
import queue
import subprocess
import re
import threading
import time
from typing import Optional
import websocket

from brain_client.input_types import InputDevice


DEFAULT_SAMPLE_RATE = 24_000
DEFAULT_CHANNELS = 1
DTYPE = 'int16'
CHUNK_DURATION_SEC = 0.02


class ArmMicroInput(InputDevice):
    """ARM Microphone input device using PulseAudio."""

    def __init__(self, logger=None):
        super().__init__(logger)
        self.mic = None
        self.client = None
        self._stop_evt = threading.Event()
        self._audio_thread = None
        
        # Load configuration from .env file
        self._load_config()

    @property
    def name(self) -> str:
        return "arm_micro"
    
    def _load_config(self):
        """Load configuration from .env file in INNATE_OS_ROOT."""
        self.config = {}
        
        innate_os_root = os.getenv('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
        env_file_path = os.path.join(innate_os_root, '.env')
        
        if os.path.exists(env_file_path):
            try:
                with open(env_file_path) as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#') and '=' in line:
                            key, value = line.split('=', 1)
                            self.config[key.strip()] = value.strip()
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Failed to load config: {e}")
        
        # Env vars override .env file
        for key in ['OPENAI_API_KEY', 'OPENAI_REALTIME_MODEL', 'OPENAI_REALTIME_URL', 
                    'OPENAI_TRANSCRIBE_MODEL']:
            if key in os.environ:
                self.config[key] = os.environ[key]

    def on_open(self):
        """Start microphone and connect to OpenAI."""
        try:
            # Auto-detect PulseAudio source
            detected_source = self._detect_pulseaudio_source()
            
            # Use ParecStreamer for PulseAudio capture
            self.mic = ParecStreamer(self.logger if self.logger else type('obj', (object,), {'error': lambda self, x: print(x)})())
            self.mic.start(source=detected_source, 
                          sample_rate=DEFAULT_SAMPLE_RATE, 
                          channels=DEFAULT_CHANNELS)
            
            # Connect to OpenAI
            api_key = self.config.get('OPENAI_API_KEY', '')
            if not api_key:
                if self.logger:
                    self.logger.error("❌ OPENAI_API_KEY not set")
                return
            
            model = self.config.get('OPENAI_REALTIME_MODEL', 'gpt-4o-realtime-preview')
            base_url = self.config.get('OPENAI_REALTIME_URL', 'wss://api.openai.com/v1/realtime')
            wss_url = f"{base_url}?model={model}"
            
            headers = [
                f"Authorization: Bearer {api_key}",
                "OpenAI-Beta: realtime=v1",
            ]
            
            vad_threshold = 0.5
            self.client = RealtimeClient(wss_url, headers, self.logger if self.logger else type('obj', (object,), {'info': lambda self, x: print(x), 'error': lambda self, x: print(x), 'warn': lambda self, x: print(x)})(), vad_threshold)
            
            # Wire up transcript callback
            def on_message(ws, message: str):
                try:
                    event = json.loads(message)
                except Exception:
                    return
                etype = event.get("type")
                
                if etype == "input_audio_buffer.speech_started":
                    if self.logger:
                        self.logger.info("🎤 Speech detected")
                elif etype == "input_audio_buffer.speech_stopped":
                    if self.logger:
                        self.logger.info("🔇 Speech stopped")
                elif etype == "conversation.item.input_audio_transcription.completed":
                    transcript = event.get("transcript", "")
                    if transcript and self.is_active():
                        self._on_transcript(transcript)
                elif etype == "error":
                    error_code = event.get("error", {}).get("code", "")
                    if error_code != "input_audio_buffer_commit_empty" and self.logger:
                        self.logger.error(f"❌ OpenAI error: {error_code}")
            
            self.client._on_message = on_message
            self.client.start()
            
            # Start audio thread
            self._stop_evt.clear()
            
            def audio_loop():
                if not self.client.wait_until_connected(timeout=10):
                    if self.logger:
                        self.logger.error("WebSocket didn't connect in time")
                    return
                
                while not self._stop_evt.is_set():
                    try:
                        chunk = self.mic.queue.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    try:
                        # Skip sending while ducking (robot is speaking)
                        if self._is_robot_talking:
                            continue
                        
                        payload = {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(chunk).decode("ascii"),
                        }
                        self.client.send_json(payload)
                    except Exception as e:
                        if self.logger:
                            self.logger.error(f"audio loop error: {e}")
            
            self._audio_thread = threading.Thread(target=audio_loop, daemon=True)
            self._audio_thread.start()
                
        except Exception as e:
            if self.logger:
                self.logger.error(f"❌ Failed to open ArmMicroInput: {e}")

    def on_close(self):
        """Stop microphone and disconnect."""
        self._stop_evt.set()
        if self._audio_thread:
            self._audio_thread.join(timeout=1.0)
        
        if self.mic:
            try:
                self.mic.stop()
            except:
                pass
            self.mic = None
        
        if self.client:
            self.client.stop()
            self.client = None

    def _detect_pulseaudio_source(self) -> Optional[str]:
        """Detect available PulseAudio audio sources and select the best one."""
        sources = []
        try:
            # List PulseAudio sources
            result = subprocess.run(['pactl', 'list', 'sources', 'short'], 
                                   capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                # Format: index name module sample_spec state
                for line in result.stdout.strip().split('\n'):
                    if not line:
                        continue
                    parts = line.split('\t')
                    if len(parts) >= 2:
                        source_name = parts[1]
                        # Skip monitor sources (output monitors, not input)
                        if '.monitor' not in source_name:
                            sources.append(source_name)
                            
            if self.logger:
                self.logger.info(f"🎤 Found PulseAudio sources: {sources}")
                
        except Exception as e:
            if self.logger:
                self.logger.error(f"Failed to detect PulseAudio sources: {e}")
        
        # Try to find a suitable microphone source
        preferred_source = None
        
        # Look for USB microphones first
        for src in sources:
            src_lower = src.lower()
            if 'usb' in src_lower and ('mic' in src_lower or 'input' in src_lower):
                preferred_source = src
                break
        
        # Fall back to any input source that's not a monitor
        if not preferred_source:
            for src in sources:
                src_lower = src.lower()
                if 'input' in src_lower or 'mic' in src_lower:
                    preferred_source = src
                    break
        
        # Last resort: use first non-monitor source
        if not preferred_source and sources:
            preferred_source = sources[0]
        
        if self.logger and preferred_source:
            self.logger.info(f"🎤 Selected PulseAudio source: {preferred_source}")
        
        return preferred_source
    
    def _on_transcript(self, text: str):
        """Called when transcript is ready."""
        if text:
            if self.logger:
                self.logger.info(f"🎤 Transcript: {text}")
            
            self.send_data(text, data_type="chat_in")


class ParecStreamer:
    """Audio streamer using PulseAudio's parec command."""
    
    def __init__(self, logger):
        self.queue: "queue.Queue[bytes]" = queue.Queue(maxsize=100)
        self._proc: Optional[subprocess.Popen] = None
        self.logger = logger
        self.sample_rate = DEFAULT_SAMPLE_RATE
        self.channels = DEFAULT_CHANNELS
        self._reader_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self, source: Optional[str] = None, sample_rate: int = DEFAULT_SAMPLE_RATE, channels: int = DEFAULT_CHANNELS):
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        
        # parec for raw PCM 16-bit output to stdout
        cmd = [
            'parec',
            '--format=s16le',
            f'--rate={self.sample_rate}',
            f'--channels={self.channels}',
            '--raw',
        ]
        
        # Add source if specified
        if source:
            cmd.extend(['-d', str(source)])
        
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        if not self._proc or not self._proc.stdout:
            raise RuntimeError('Failed to start parec process')

        def reader():
            try:
                # Calculate chunk size for ~20ms of audio
                bytes_per_sample = 2  # 16-bit = 2 bytes
                frame_bytes = int(self.sample_rate * CHUNK_DURATION_SEC * self.channels * bytes_per_sample)
                while not self._stop.is_set():
                    buf = self._proc.stdout.read(frame_bytes)
                    if not buf:
                        time.sleep(0.01)
                        continue
                    try:
                        self.queue.put_nowait(buf)
                    except queue.Full:
                        pass
            except Exception as e:
                self.logger.error(f"parec reader error: {e}")
                
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


class RealtimeClient:
    """WebSocket client for OpenAI Realtime API."""
    
    def __init__(self, url: str, headers: list[str], logger, vad_threshold: float = 0.5):
        self.url = url
        self.headers = headers
        self.ws: Optional[websocket.WebSocketApp] = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = threading.Event()
        self.logger = logger
        self.vad_threshold = vad_threshold

    def start(self):
        self.ws = websocket.WebSocketApp(
            self.url,
            header=self.headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        t = threading.Thread(
            target=self.ws.run_forever,
            kwargs={"ping_interval": 30, "ping_timeout": 10},
            daemon=True,
        )
        t.start()

    def stop(self):
        self._stop.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self._connected.clear()

    def wait_until_connected(self, timeout: float = 10.0) -> bool:
        return self._connected.wait(timeout=timeout)

    def send_json(self, payload: dict):
        data = json.dumps(payload)
        with self._send_lock:
            if self.ws and self._connected.is_set():
                try:
                    self.ws.send(data)
                except Exception as e:
                    self.logger.error(f"[send_json] {e}")

    # --- callbacks ---
    def _on_open(self, ws):
        self._connected.set()
        transcribe_model = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
        session_update = {
            "type": "session.update",
            "session": {
                "input_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": transcribe_model,
                    "language": "en"
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": float(self.vad_threshold),
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 700,
                    "create_response": False,
                },
                "instructions": "Transcribe user audio only in English; do not reply.",
            },
        }
        self.send_json(session_update)

    def _on_message(self, ws, message: str):
        # Default handler - node overrides this
        pass

    def _on_error(self, ws, error):
        self.logger.error(f"[ws error] {error}")

    def _on_close(self, ws, status_code, msg):
        self.logger.warn("WebSocket closed")
        self._connected.clear()
