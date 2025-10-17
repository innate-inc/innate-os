#!/usr/bin/env python3
"""
Microphone Input Device

Connects to microphone hardware and OpenAI's realtime API to get voice transcripts.
This is a pure Python class with NO ROS dependencies.
"""
import base64
import json
import os
import queue
import threading
import time
from typing import Dict, Any, Optional
import sounddevice as sd
import websocket

from brain_client.input_types import InputDevice


class MicroInput(InputDevice):
    """
    Microphone input device that connects directly to hardware and OpenAI.
    
    Replaces the functionality of voice_client_node, but as a pure Python
    input device that can be activated/deactivated by directives.
    """

    def __init__(self, logger=None):
        super().__init__(logger)
        self.mic = None
        self.ws_client = None
        self._stop_event = threading.Event()
        self._audio_thread = None
        
        # Configuration (could be made configurable)
        self.sample_rate = int(os.getenv('MIC_SAMPLE_RATE', '24000'))
        self.channels = int(os.getenv('MIC_CHANNELS', '1'))
        self.vad_threshold = float(os.getenv('VAD_THRESHOLD', '0.5'))

    @property
    def name(self) -> str:
        return "micro"

    def on_open(self):
        """Start microphone and connect to OpenAI."""
        if self.logger:
            self.logger.info("🎤 MicroInput opening - connecting to microphone and OpenAI")
        
        try:
            # Start microphone
            self.mic = MicrophoneStream(self.sample_rate, self.channels, self.logger)
            self.mic.start()
            
            # Connect to OpenAI
            api_key = os.getenv('OPENAI_API_KEY', '')
            if not api_key:
                if self.logger:
                    self.logger.error("❌ OPENAI_API_KEY not set")
                return
            
            model = os.getenv('OPENAI_REALTIME_MODEL', 'gpt-4o-realtime-preview')
            base_url = os.getenv('OPENAI_REALTIME_URL', 'wss://api.openai.com/v1/realtime')
            wss_url = f"{base_url}?model={model}"
            
            self.ws_client = OpenAIRealtimeClient(
                wss_url, api_key, self.vad_threshold, 
                self._on_transcript, self.logger
            )
            self.ws_client.connect()
            
            # Start audio streaming thread
            self._stop_event.clear()
            self._audio_thread = threading.Thread(target=self._audio_loop, daemon=True)
            self._audio_thread.start()
            
            if self.logger:
                self.logger.info("✅ MicroInput ready")
                
        except Exception as e:
            if self.logger:
                self.logger.error(f"❌ Failed to open MicroInput: {e}")

    def on_close(self):
        """Stop microphone and disconnect from OpenAI."""
        if self.logger:
            self.logger.info("🎤 MicroInput closing")
        
        # Stop audio thread
        self._stop_event.set()
        if self._audio_thread:
            self._audio_thread.join(timeout=1.0)
        
        # Stop microphone
        if self.mic:
            self.mic.stop()
            self.mic = None
        
        # Disconnect websocket
        if self.ws_client:
            self.ws_client.disconnect()
            self.ws_client = None

    def _audio_loop(self):
        """Background thread that streams audio to OpenAI."""
        if not self.ws_client or not self.ws_client.wait_connected(timeout=10):
            if self.logger:
                self.logger.error("WebSocket didn't connect in time")
            return
        
        while not self._stop_event.is_set():
            try:
                # Get audio chunk from microphone
                chunk = self.mic.read(timeout=0.1)
                if chunk:
                    # Send to OpenAI
                    self.ws_client.send_audio(chunk)
            except queue.Empty:
                continue
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Audio loop error: {e}")
                break

    def _on_transcript(self, text: str):
        """Called by websocket client when transcript is ready."""
        if text and self.is_active():
            # Send to agent
            self.send_data({
                "text": text,
                "sender": "user",
                "source": "microphone",
                "timestamp": time.time()
            }, data_type="chat_in")
            
            if self.logger:
                self.logger.info(f"🎤 [MicroInput] Transcript: {text}")


class MicrophoneStream:
    """Simple microphone streamer using sounddevice."""
    
    def __init__(self, sample_rate: int, channels: int, logger):
        self.sample_rate = sample_rate
        self.channels = channels
        self.logger = logger
        self.queue = queue.Queue(maxsize=50)
        self._stream: Optional[sd.RawInputStream] = None
    
    def _callback(self, indata, frames, time_info, status):
        if status and self.logger:
            self.logger.warning(f"[Audio] {status}")
        try:
            self.queue.put_nowait(bytes(indata))
        except queue.Full:
            pass
    
    def start(self):
        self._stream = sd.RawInputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype='int16',
            callback=self._callback
        )
        self._stream.start()
        if self.logger:
            self.logger.info(f"🎙️ Microphone started: {self.sample_rate}Hz, {self.channels}ch")
    
    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
    
    def read(self, timeout=None):
        return self.queue.get(timeout=timeout)


class OpenAIRealtimeClient:
    """WebSocket client for OpenAI Realtime API."""
    
    def __init__(self, url: str, api_key: str, vad_threshold: float, 
                 on_transcript_callback, logger):
        self.url = url
        self.api_key = api_key
        self.vad_threshold = vad_threshold
        self.on_transcript = on_transcript_callback
        self.logger = logger
        self.ws: Optional[websocket.WebSocketApp] = None
        self._connected = threading.Event()
        self._stop = threading.Event()
    
    def connect(self):
        headers = [
            f"Authorization: Bearer {self.api_key}",
            "OpenAI-Beta: realtime=v1",
        ]
        
        self.ws = websocket.WebSocketApp(
            self.url,
            header=headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        
        thread = threading.Thread(
            target=self.ws.run_forever,
            kwargs={"ping_interval": 30, "ping_timeout": 10},
            daemon=True
        )
        thread.start()
    
    def disconnect(self):
        self._stop.set()
        if self.ws:
            try:
                self.ws.close()
            except:
                pass
        self._connected.clear()
    
    def wait_connected(self, timeout=10):
        return self._connected.wait(timeout=timeout)
    
    def send_audio(self, audio_bytes: bytes):
        if self.ws and self._connected.is_set():
            try:
                payload = {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(audio_bytes).decode("ascii"),
                }
                self.ws.send(json.dumps(payload))
            except Exception as e:
                if self.logger:
                    self.logger.error(f"Failed to send audio: {e}")
    
    def _on_open(self, ws):
        self._connected.set()
        # Configure session
        session_update = {
            "type": "session.update",
            "session": {
                "input_audio_format": "pcm16",
                "input_audio_transcription": {
                    "model": "whisper-1",
                    "language": "en"
                },
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": self.vad_threshold,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 700,
                    "create_response": False,
                },
                "instructions": "Transcribe user audio only in English; do not reply.",
            },
        }
        self.ws.send(json.dumps(session_update))
        if self.logger:
            self.logger.info("✅ Connected to OpenAI")
    
    def _on_message(self, ws, message: str):
        try:
            event = json.loads(message)
            event_type = event.get("type")
            
            if event_type == "input_audio_buffer.speech_started":
                if self.logger:
                    self.logger.info("🎤 Speech detected")
            elif event_type == "input_audio_buffer.speech_stopped":
                if self.logger:
                    self.logger.info("🔇 Speech stopped")
            elif event_type == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript", "")
                if transcript:
                    self.on_transcript(transcript)
            elif event_type == "error":
                error_code = event.get("error", {}).get("code", "")
                if error_code != "input_audio_buffer_commit_empty" and self.logger:
                    self.logger.error(f"❌ OpenAI error: {error_code}")
        except Exception as e:
            if self.logger:
                self.logger.error(f"Error processing message: {e}")
    
    def _on_error(self, ws, error):
        if self.logger:
            self.logger.error(f"WebSocket error: {error}")
    
    def _on_close(self, ws, status_code, msg):
        if self.logger:
            self.logger.warning("WebSocket closed")
        self._connected.clear()


