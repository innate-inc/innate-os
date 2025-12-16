#!/usr/bin/env python3
"""
Microphone Input Device

Connects to microphone hardware and OpenAI's realtime API to get voice transcripts.
This is a pure Python class with NO ROS dependencies.

Uses the EXACT same logic as voice_client_node.py but as an input device.
"""
import base64
import json
import os
import queue
import threading
import time
from typing import Optional
import sounddevice as sd
import websocket
import sys
from pathlib import Path

from brain_client.input_types import InputDevice
import subprocess
import re

# Add client library to path (add parent dir so 'client' package is importable)
innate_os_root = os.getenv('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
client_path = Path(innate_os_root) / 'client'
if client_path.exists() and str(innate_os_root) not in sys.path:
    sys.path.insert(0, str(innate_os_root))

try:
    from client.adapters.openai_adapter import ProxyOpenAIClient
    PROXY_AVAILABLE = True
except ImportError:
    PROXY_AVAILABLE = False


DEFAULT_SAMPLE_RATE = 24_000
DEFAULT_CHANNELS = 1
DTYPE = 'int16'
CHUNK_DURATION_SEC = 0.02


class MicroInput(InputDevice):
    """Microphone input device - exact copy of voice_client logic."""

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
        return "micro"
    
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
                    'OPENAI_TRANSCRIBE_MODEL', 'INNATE_PROXY_URL', 'INNATE_SERVICE_KEY']:
            if key in os.environ:
                self.config[key] = os.environ[key]

    def on_open(self):
        """Start microphone and connect to OpenAI - exact copy of voice_client logic."""
        try:
            # Auto-detect and start microphone (exact same as voice_client)
            detected_device = self._detect_audio_device()
            if not detected_device:
                detected_device = 'default'

            # Use ArecordStreamer for plughw devices (like voice_client does)
            self.mic = ArecordStreamer(self.logger if self.logger else type('obj', (object,), {'error': lambda self, x: print(x)})())
            self.mic.start(
                device=detected_device if detected_device != 'default' else 'default',
                sample_rate=DEFAULT_SAMPLE_RATE,
                channels=DEFAULT_CHANNELS
            )

            # Connect to OpenAI via proxy if available, otherwise direct
            proxy_url = self.config.get('INNATE_PROXY_URL', '')
            innate_service_key = self.config.get('INNATE_SERVICE_KEY', '')

            if PROXY_AVAILABLE and proxy_url and innate_service_key:
                # Use proxy client
                if self.logger:
                    self.logger.info("🔗 Using OpenAI proxy client")
                model = self.config.get('OPENAI_REALTIME_MODEL', 'gpt-4o-realtime-preview')
                vad_threshold = 0.5

                # Use proxy client's sync interface
                proxy_client = ProxyOpenAIClient(
                    proxy_url=proxy_url,
                    innate_service_key=innate_service_key
                )

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
                        error_obj = event.get("error", {})
                        error_code = error_obj.get("code", "")
                        error_msg = error_obj.get("message", "")
                        error_param = error_obj.get("param", "")
                        if error_code != "input_audio_buffer_commit_empty" and self.logger:
                            self.logger.error(f"❌ OpenAI error: {error_code} - {error_msg} (param: {error_param})")

                def on_open():
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
                                "threshold": float(vad_threshold),
                                "prefix_padding_ms": 300,
                                "silence_duration_ms": 700,
                                "create_response": False,
                            },
                            "instructions": "Transcribe user audio only in English; do not reply.",
                        },
                    }
                    self.client.send_json(session_update)

                def on_error(error):
                    if self.logger:
                        self.logger.error(f"[ws error] {error}")

                def on_close():
                    if self.logger:
                        self.logger.warn("WebSocket closed")

                # Create sync connection using proxy client
                self.client = proxy_client.realtime.connect_sync(
                    model=model,
                    on_message=on_message,
                    on_open=on_open,
                    on_error=on_error,
                    on_close=on_close,
                )
                # Start the connection
                self.client.start()
            else:
                # Fallback to direct OpenAI connection
                api_key = self.config.get('OPENAI_API_KEY', '')
                if not api_key:
                    if self.logger:
                        self.logger.error("❌ Neither proxy config (INNATE_PROXY_URL/INNATE_SERVICE_KEY) nor OPENAI_API_KEY set")
                    return

                if self.logger:
                    self.logger.info("🔗 Using direct OpenAI connection (proxy not configured)")

                model = self.config.get('OPENAI_REALTIME_MODEL', 'gpt-4o-realtime-preview')
                base_url = self.config.get('OPENAI_REALTIME_URL', 'wss://api.openai.com/v1/realtime')
                wss_url = f"{base_url}?model={model}"

                headers = [
                    f"Authorization: Bearer {api_key}",
                    "OpenAI-Beta: realtime=v1",
                ]

                vad_threshold = 0.5
                self.client = RealtimeClient(
                    wss_url,
                    headers,
                    self.logger if self.logger else type('obj', (object,), {
                        'info': lambda self, x: print(x),
                        'error': lambda self, x: print(x),
                        'warn': lambda self, x: print(x)
                    })(),
                    vad_threshold
                )

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

            # Start audio thread (exact same as voice_client)
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
                self.logger.error(f"❌ Failed to open MicroInput: {e}")

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
        """Detect and list available audio capture devices - exact copy from voice_client."""
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
        except Exception as e:
            pass
        
        # Try to find a suitable microphone device
        preferred_device = None
        
        # Look for USB microphones (usually better quality)
        for dev in devices:
            name_lower = dev['name'].lower()
            if 'mic' in name_lower and 'usb' in name_lower:
                preferred_device = dev
                break
        
        # Fall back to any mic
        if not preferred_device:
            for dev in devices:
                if 'mic' in dev['name'].lower():
                    preferred_device = dev
                    break
        
        # Fall back to camera audio
        if not preferred_device:
            for dev in devices:
                if 'camera' in dev['name'].lower() or 'webcam' in dev['name'].lower():
                    preferred_device = dev
                    break
        
        # Last resort: use first available device
        if not preferred_device and devices:
            preferred_device = devices[0]
        
        return preferred_device['id'] if preferred_device else None
    
    def _on_transcript(self, text: str):
        """Called when transcript is ready."""
        if text:
            if self.logger:
                self.logger.info(f"🎤 Transcript: {text}")
            
            self.send_data(text, data_type="chat_in")


# ========== EXACT COPIES FROM voice_client_node.py ==========
class SyncRealtimeConnection:
    """
    Wrapper that bridges async websockets (from proxy client) with sync websocket-client API.
    This allows the existing RealtimeClient interface to work with the proxy.
    """
    def __init__(self, proxy_url: str, innate_service_key: str, model: str, logger, vad_threshold: float = 0.5):
        self.proxy_url = proxy_url
        self.innate_service_key = innate_service_key
        self.model = model
        self.logger = logger
        self.vad_threshold = vad_threshold
        self.ws = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._proxy_client = None
        self._ws_connection = None
        self._message_thread = None
        self._on_message = None  # Will be set by caller
        
    def start(self):
        """Start the WebSocket connection via proxy."""
        import asyncio
        
        def run_async():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                self._proxy_client = ProxyOpenAIClient(
                    proxy_url=self.proxy_url,
                    innate_service_key=self.innate_service_key
                )
                
                # Set up message handler callback
                async def on_message_handler(ws, message: str):
                    """Async message handler that calls sync callback."""
                    if self._on_message:
                        self._on_message(None, message)
                
                # Connect via proxy
                self._ws_connection = loop.run_until_complete(
                    self._proxy_client.realtime.connect(
                        model=self.model,
                        on_message=on_message_handler
                    )
                )
                self._connected.set()
                self._on_open_wrapper()
                
                # Keep event loop running to handle messages
                # The on_message_handler will be called by the proxy client's message handler
                while not self._stop.is_set() and self._connected.is_set():
                    loop.run_until_complete(asyncio.sleep(0.1))
                    
            except Exception as e:
                self.logger.error(f"[proxy ws error] {e}")
                self._connected.clear()
            finally:
                try:
                    if self._ws_connection:
                        loop.run_until_complete(self._ws_connection.close())
                except Exception:
                    pass
                loop.close()
        
        self._message_thread = threading.Thread(target=run_async, daemon=True)
        self._message_thread.start()
    
    def stop(self):
        """Stop the WebSocket connection."""
        self._stop.set()
        if self._ws_connection:
            import asyncio
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(self._ws_connection.close())
                loop.close()
            except Exception:
                pass
        self._connected.clear()
    
    def wait_until_connected(self, timeout: float = 10.0) -> bool:
        """Wait for connection to be established."""
        return self._connected.wait(timeout=timeout)
    
    def send_json(self, payload: dict):
        """Send JSON payload via WebSocket."""
        import asyncio
        data = json.dumps(payload)
        with self._send_lock:
            if self._ws_connection and self._connected.is_set():
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    loop.run_until_complete(self._ws_connection.send(data))
                    loop.close()
                except Exception as e:
                    self.logger.error(f"[send_json] {e}")
    
    def _on_open_wrapper(self):
        """Called when WebSocket opens."""
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
    


# ========== EXACT COPIES FROM voice_client_node.py ==========

class ArecordStreamer:
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
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
        if not self._proc or not self._proc.stdout:
            raise RuntimeError('Failed to start arecord process')

        def reader():
            try:
                # 50ms chunks at sample_rate, channels
                bytes_per_sample = 2
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


class RealtimeClient:
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
