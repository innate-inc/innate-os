#!/usr/bin/env python3
"""
Microphone Input Device

Connects to microphone hardware and OpenAI's realtime API to get voice transcripts.
This is a pure Python class with NO ROS dependencies.

Uses proxy services via self.proxy (injected by InputManager).
"""
import base64
import json
import queue
import threading
import time
from typing import Optional, TYPE_CHECKING
import sounddevice as sd
import subprocess
import re

from brain_client.input_types import InputDevice

if TYPE_CHECKING:
    from brain_client.client.proxy_client import ProxyClient


DEFAULT_SAMPLE_RATE = 24_000
DEFAULT_CHANNELS = 1
DTYPE = 'int16'
CHUNK_DURATION_SEC = 0.02


class MicroInput(InputDevice):
    """
    Microphone input device.
    
    Connects to OpenAI Realtime API via proxy (self.proxy.openai.realtime).
    Config comes from self.proxy.config (set by InputManagerNode).
    
    Supports "ducking" - suppresses audio while robot is speaking.
    """

    def __init__(self, logger=None, proxy: "ProxyClient" = None):
        super().__init__(logger, proxy)
        self.mic = None
        self.client = None
        self._stop_evt = threading.Event()
        self._audio_thread = None
        self._is_robot_talking = False  # For ducking (mic-specific)

    @property
    def name(self) -> str:
        return "micro"
    
    def set_tts_playing(self, is_playing: bool):
        """
        Called when TTS (text-to-speech) status changes.
        
        Implements "ducking" - suppressing mic input while robot speaks.
        
        Args:
            is_playing: True if robot is speaking, False otherwise
        """
        self._is_robot_talking = is_playing

    def on_open(self):
        """Start microphone and connect to OpenAI via proxy."""
        # Check proxy is available
        if not self.proxy or not self.proxy.is_available():
            if self.logger:
                self.logger.error("❌ Proxy not configured - cannot start microphone input")
            return
        
        try:
            # Auto-detect and start microphone
            detected_device = self._detect_audio_device()
            if not detected_device:
                detected_device = 'default'
            
            if self.logger:
                self.logger.info(f"🎙️ Using audio device: {detected_device}")
            
            # Create dummy logger if needed
            mic_logger = self.logger if self.logger else type('obj', (object,), {'error': lambda self, x: print(x)})()
            self.mic = ArecordStreamer(mic_logger)
            self.mic.start(
                device=detected_device if detected_device != 'default' else 'default',
                          sample_rate=DEFAULT_SAMPLE_RATE, 
                channels=DEFAULT_CHANNELS
            )
            
            if self.logger:
                self.logger.info(f"🎙️ Microphone started (rate: {DEFAULT_SAMPLE_RATE}, channels: {DEFAULT_CHANNELS})")
            
            # Connect via proxy
            self._connect_via_proxy()
                
        except Exception as e:
            if self.logger:
                self.logger.error(f"❌ Failed to start microphone: {e}")
            import traceback
            traceback.print_exc()

    def _connect_via_proxy(self):
        """Connect to OpenAI Realtime API via proxy."""
        if self.logger:
            self.logger.info("🔗 Connecting to OpenAI via proxy...")
        
        # Get config from proxy (injected by InputManager)
        model = self.proxy.config.get('openai_realtime_model', 'gpt-4o-realtime-preview')
        transcribe_model = self.proxy.config.get('openai_transcribe_model', 'gpt-4o-mini-transcribe')
        vad_threshold = 0.3  # Lower = more sensitive to speech
        
        # Wire up transcript callback
        def on_message(ws, message: str):
            try:
                event = json.loads(message)
            except Exception:
                if self.logger:
                    self.logger.error(f"Failed to parse message: {message[:200]}")
                return
            
            etype = event.get("type")
            
            # Log full session.updated to verify config was accepted
            if etype == "session.updated" and self.logger:
                session = event.get("session", {})
                transcription = session.get("input_audio_transcription", {})
                turn_detection = session.get("turn_detection", {})
                self.logger.info(f"📋 Session updated - transcription: {transcription}, turn_detection: {turn_detection}")
            
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
                error_obj = event.get("error", {})
                error_code = error_obj.get("code", "")
                error_msg = error_obj.get("message", "")
                error_param = error_obj.get("param", "")
                if error_code != "input_audio_buffer_commit_empty" and self.logger:
                    self.logger.error(f"❌ OpenAI error: {error_code} - {error_msg} (param: {error_param})")
            else:
                # Log other message types for debugging
                if self.logger:
                    self.logger.info(f"📨 OpenAI event: {etype}")
        
        def on_open_callback():
            if self.logger:
                self.logger.info("📤 WebSocket opened, sending session.update...")
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
                        "threshold": float(vad_threshold),
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 700,
                        "create_response": False,
                    },
                    "instructions": "Transcribe user audio only in English; do not reply.",
                },
            }
            if self.logger:
                self.logger.info(f"📤 Session config: model={transcribe_model}, vad_threshold={vad_threshold}")
            self.client.send_json(session_update)
            if self.logger:
                self.logger.info("📤 session.update sent")
        
        def on_error(error):
            if self.logger:
                self.logger.error(f"[ws error] {error}")
        
        def on_close():
            if self.logger:
                self.logger.warning("WebSocket closed")
        
        # Use proxy's OpenAI adapter
        self.client = self.proxy.openai.realtime.connect_sync(
            model=model,
            on_message=on_message,
            on_open=on_open_callback,
            on_error=on_error,
            on_close=on_close,
        )
        self.client.start()
        
        # Start audio streaming thread
        self._start_audio_thread()
        
        if self.logger:
            self.logger.info(f"✅ Connected to OpenAI Realtime (model: {model})")

    def _start_audio_thread(self):
        """Start the audio streaming thread."""
        self._stop_evt.clear()
        
        def audio_loop():
            if not self.client.wait_until_connected(timeout=10):
                if self.logger:
                    self.logger.error("WebSocket didn't connect in time")
                return
            
            if self.logger:
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
                    if empty_count == 50 and self.logger:
                        self.logger.warning("⚠️ No audio chunks received (queue empty for 5s)")
                    continue
                
                try:
                    # Skip sending while ducking (robot is speaking)
                    if self._is_robot_talking:
                        if not ducking_logged and self.logger:
                            self.logger.info("🔇 Ducking active - not sending audio")
                        ducking_logged = True
                        continue
                    ducking_logged = False
                    
                    payload = {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(chunk).decode("ascii"),
                    }
                    self.client.send_json(payload)
                    chunks_sent += 1

                    # Log periodically (much less frequently)
                    if chunks_sent == 100 and self.logger:
                        self.logger.info(f"🎧 Streaming audio ({chunks_sent} chunks)")
                    elif chunks_sent % 2500 == 0 and self.logger:
                        self.logger.info(f"🎧 Audio chunks sent: {chunks_sent}")
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"audio loop error: {e}")
                    import traceback
                    self.logger.error(traceback.format_exc())
        
        self._audio_thread = threading.Thread(target=audio_loop, daemon=True)
        self._audio_thread.start()

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

    def _detect_audio_device(self):
        """Detect and list available audio capture devices."""
        devices = []
        try:
            result = subprocess.run(['arecord', '-l'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                pattern = r'card (\d+):.*?\[([^\]]+)\].*?device (\d+):'
                for match in re.finditer(pattern, result.stdout):
                    card_num = match.group(1)
                    card_name = match.group(2)
                    device_num = match.group(3)
                    device_id = f"plughw:{card_num},{device_num}"
                    devices.append({
                        'card': card_num,
                        'device': device_num,
                        'name': card_name,
                        'id': device_id
                    })
        except Exception:
            pass
        
        # Try to find a suitable microphone device
        preferred_device = None
        
        if self.logger:
            self.logger.info(f"🔍 Found {len(devices)} audio devices: {[d['name'] for d in devices]}")
        
        # Look for USB microphones (usually better quality)
        for dev in devices:
            name_lower = dev['name'].lower()
            if 'mic' in name_lower and 'usb' in name_lower:
                preferred_device = dev
                break
        
        # Look for USB Sound Device (common external mic name)
        if not preferred_device:
            for dev in devices:
                name_lower = dev['name'].lower()
                if 'sound' in name_lower and ('usb' in name_lower or 'pnp' in name_lower):
                    preferred_device = dev
                    break
        
        # Fall back to any mic
        if not preferred_device:
            for dev in devices:
                if 'mic' in dev['name'].lower():
                    preferred_device = dev
                    break
        
        # Fall back to any USB audio device (but NOT camera)
        if not preferred_device:
            for dev in devices:
                name_lower = dev['name'].lower()
                if 'usb' in name_lower and 'camera' not in name_lower and 'webcam' not in name_lower:
                    preferred_device = dev
                    break
        
        # Fall back to camera audio (least preferred)
        if not preferred_device:
            for dev in devices:
                if 'camera' in dev['name'].lower() or 'webcam' in dev['name'].lower():
                    preferred_device = dev
                    break
        
        # Last resort: use first available device
        if not preferred_device and devices:
            preferred_device = devices[0]
        
        if self.logger and preferred_device:
            self.logger.info(f"🎙️ Selected audio device: {preferred_device['name']} ({preferred_device['id']})")
        
        return preferred_device['id'] if preferred_device else None
    
    def _on_transcript(self, text: str):
        """Called when transcript is ready."""
        if text:
            if self.logger:
                self.logger.info(f"🎤 Transcript: {text}")
            
            self.send_data(text, data_type="chat_in")


# ========== Audio Streaming Helpers ==========

class ArecordStreamer:
    """Streams audio from ALSA via arecord subprocess."""
    
    def __init__(self, logger):
        self.queue: "queue.Queue[bytes]" = queue.Queue(maxsize=100)
        self._proc: Optional[subprocess.Popen] = None
        self.logger = logger
        self.sample_rate = DEFAULT_SAMPLE_RATE
        self.channels = DEFAULT_CHANNELS
        self._reader_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self, device: str = 'default', sample_rate: int = DEFAULT_SAMPLE_RATE, channels: int = DEFAULT_CHANNELS):
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        # arecord raw PCM 16-bit, stdout
        cmd = [
            'arecord',
            '-D', str(device),
            '-f', 'S16_LE',
            '-r', str(self.sample_rate),
            '-c', str(self.channels),
            '-t', 'raw',
            '-q',  # quiet
            '-'
        ]
        self.logger.info(f"🎙️ Starting arecord: {' '.join(cmd)}")
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        if not self._proc or not self._proc.stdout:
            raise RuntimeError('Failed to start arecord process')
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


class MicStreamer:
    """Streams audio via sounddevice (PortAudio)."""
    
    def __init__(self, logger):
        self.queue: "queue.Queue[bytes]" = queue.Queue(maxsize=50)
        self._stream: Optional[sd.RawInputStream] = None
        self.logger = logger
        self.sample_rate = DEFAULT_SAMPLE_RATE
        self.channels = DEFAULT_CHANNELS

    def _callback(self, indata, frames, time_info, status):
        if status:
            self.logger.warn(f"[PortAudio] {status}")
        try:
            self.queue.put_nowait(bytes(indata))
        except queue.Full:
            pass

    def start(self, device: Optional[str] = None, sample_rate: int = DEFAULT_SAMPLE_RATE, channels: int = DEFAULT_CHANNELS):
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        frames_per_chunk = int(self.sample_rate * CHUNK_DURATION_SEC)
        kwargs = dict(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype=DTYPE,
            blocksize=frames_per_chunk,
            callback=self._callback,
        )
        if device:
            try:
                kwargs['device'] = int(device) if isinstance(device, str) and device.isdigit() else device
            except Exception:
                kwargs['device'] = device
        
        self._stream = sd.RawInputStream(**kwargs)
        self._stream.start()
