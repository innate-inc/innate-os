#!/usr/bin/env python3
import base64
import json
import os
import queue
import threading
import time
from typing import Optional
import subprocess

import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import SetBool

import sounddevice as sd
import websocket


DEFAULT_SAMPLE_RATE = 24_000
DEFAULT_CHANNELS = 1
DTYPE = 'int16'
CHUNK_DURATION_SEC = 0.02  # 20ms frames improve VAD onset and latency


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
        self.logger.info("✅ Connected to OpenAI")

    def _on_message(self, ws, message: str):
        # Default handler - node overrides this
        pass

    def _on_error(self, ws, error):
        self.logger.error(f"[ws error] {error}")

    def _on_close(self, ws, status_code, msg):
        self.logger.warn("WebSocket closed")
        self._connected.clear()


def periodic_commit(client: RealtimeClient, stop_evt: threading.Event, logger, interval_sec: float = 1.0, should_commit_fn=None):
    # Some deployments require explicit commits to finalize segments
    if not client.wait_until_connected(timeout=10):
        logger.error("[commit] WebSocket didn't open in time")
        return
    while not stop_evt.is_set():
        try:
            if should_commit_fn is None or bool(should_commit_fn()):
                client.send_json({"type": "input_audio_buffer.commit"})
                logger.debug("Committed audio buffer to OpenAI")
        except Exception:
            pass
        time.sleep(interval_sec)


class AudioTransformer:
    def __init__(self, logger, in_channels: int, in_rate: int, out_channels: int, out_rate: int):
        self.logger = logger
        self.in_channels = int(in_channels)
        self.in_rate = int(in_rate)
        self.out_channels = int(out_channels)
        self.out_rate = int(out_rate)
        self._residual = b""

    def _downmix_to_mono(self, samples: "np.ndarray") -> "np.ndarray":
        import numpy as np
        if self.in_channels == 1 or self.out_channels != 1:
            return samples
        try:
            reshaped = samples.reshape((-1, self.in_channels)).astype(np.int32)
            mono = (reshaped.sum(axis=1) // self.in_channels).clip(-32768, 32767).astype(np.int16)
            return mono
        except Exception as e:
            self.logger.error(f"downmix error: {e}")
            return samples

    def _decimate_integer(self, samples: "np.ndarray") -> "np.ndarray":
        import numpy as np
        if self.in_rate == self.out_rate:
            return samples
        if self.in_rate % self.out_rate != 0:
            # Non-integer ratio; pass-through for now
            return samples
        factor = self.in_rate // self.out_rate
        data = samples
        if self._residual:
            try:
                prev = np.frombuffer(self._residual, dtype=np.int16)
                data = np.concatenate([prev, data])
            except Exception:
                pass
            self._residual = b""
        usable = (len(data) // factor) * factor
        if usable <= 0:
            # Not enough samples; stash and return empty
            try:
                self._residual = data.tobytes()
            except Exception:
                pass
            return np.zeros((0,), dtype=np.int16)
        head = data[:usable]
        tail = data[usable:]
        if tail.size:
            try:
                self._residual = tail.tobytes()
            except Exception:
                pass
        try:
            if factor == 2:
                # Simple 2-tap avg then decimate
                a = head[0::2].astype(np.int32)
                b = head[1::2].astype(np.int32)
                out = ((a + b) // 2).clip(-32768, 32767).astype(np.int16)
            else:
                # General block average
                reshaped = head.reshape((-1, factor)).astype(np.int32)
                out = (reshaped.mean(axis=1)).astype(np.int16)
            return out
        except Exception as e:
            self.logger.error(f"decimate error: {e}")
            return samples

    def transform(self, int16_bytes: bytes) -> bytes:
        import numpy as np
        try:
            x = np.frombuffer(int16_bytes, dtype=np.int16)
            if self.in_channels > 1:
                x = self._downmix_to_mono(x)
            if self.in_rate != self.out_rate:
                x = self._decimate_integer(x)
            return x.tobytes()
        except Exception as e:
            self.logger.error(f"transform error: {e}")
            return int16_bytes


class VoiceClientNode(Node):
    def __init__(self):
        super().__init__('voice_client_node')

        # Parameters
        self.declare_parameter('mic_device', '')
        self.declare_parameter('sample_rate', DEFAULT_SAMPLE_RATE)
        self.declare_parameter('channels', DEFAULT_CHANNELS)
        self.declare_parameter('capture_backend', 'sounddevice')  # 'sounddevice' | 'arecord'
        self.declare_parameter('openai_realtime_model', os.getenv('OPENAI_REALTIME_MODEL', 'gpt-4o-realtime-preview'))
        self.declare_parameter('openai_realtime_url', os.getenv('OPENAI_REALTIME_URL', 'wss://api.openai.com/v1/realtime'))
        # New transformation and VAD/commit params
        self.declare_parameter('target_sample_rate', 24000)
        self.declare_parameter('target_channels', 1)
        self.declare_parameter('force_resample_downmix', True)
        self.declare_parameter('vad_threshold', 0.5)
        self.declare_parameter('commit_interval_s', 0.0)  # 0 = disable periodic commits

        self.active = True

        # Chat publisher
        self.chat_in_pub = self.create_publisher(String, '/chat_in', 10)

        # Activation service
        self.set_active_srv = self.create_service(SetBool, '/voice/set_active', self.handle_set_active)

        # Optional: subscribe to chat_out for future ducking (log only)
        self.chat_out_sub = self.create_subscription(String, '/chat_out', self.chat_out_callback, 10)

        # Mic and WS client
        self.mic = None
        api_key = os.getenv('OPENAI_API_KEY', '')
        model = self.get_parameter('openai_realtime_model').get_parameter_value().string_value
        base_url = self.get_parameter('openai_realtime_url').get_parameter_value().string_value
        # Use regular realtime API with model parameter (transcription via session.update)
        wss_url = f"{base_url}?model={model}"

        headers = [
            f"Authorization: Bearer {api_key}",
            "OpenAI-Beta: realtime=v1",
        ]
        vad_threshold = float(self.get_parameter('vad_threshold').get_parameter_value().double_value if hasattr(self.get_parameter('vad_threshold').get_parameter_value(), 'double_value') else self.get_parameter('vad_threshold').value)
        self.client = RealtimeClient(wss_url, headers, self.get_logger(), vad_threshold=vad_threshold)

        # Track speech state for smart commits
        self._speech_active = False
        
        # Wire message callback to collect transcripts
        def on_message(ws, message: str):
            try:
                event = json.loads(message)
            except Exception:
                return
            etype = event.get("type")
            
            if etype == "input_audio_buffer.speech_started":
                self._speech_active = True
                self.get_logger().info("🎤 Speech detected")
            elif etype == "input_audio_buffer.speech_stopped":
                self._speech_active = False
                self.get_logger().info("🔇 Speech stopped")
            elif etype == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript", "")
                if transcript and self.active:
                    self.publish_chat_in(transcript)
            elif etype == "error":
                error_code = event.get("error", {}).get("code", "")
                if error_code != "input_audio_buffer_commit_empty":
                    self.get_logger().error(f"❌ OpenAI error: {error_code}")
        
        self.client._on_message = on_message

        # Start mic and WS
        try:
            mic_device = self.get_parameter('mic_device').get_parameter_value().string_value
            sample_rate = self.get_parameter('sample_rate').get_parameter_value().integer_value
            if sample_rate == 0:
                sample_rate = DEFAULT_SAMPLE_RATE
            channels = self.get_parameter('channels').get_parameter_value().integer_value
            if channels == 0:
                channels = DEFAULT_CHANNELS

            backend = self.get_parameter('capture_backend').get_parameter_value().string_value or 'sounddevice'

            if backend == 'arecord':
                self.mic = ArecordStreamer(self.get_logger())
                self.mic.start(device=mic_device if mic_device else 'default', sample_rate=sample_rate, channels=channels)
                self.get_logger().info(f"🎙️ Microphone started: {mic_device or 'default'}")
            else:
                self.mic = MicStreamer(self.get_logger())
                self.mic.start(device=mic_device if mic_device else None, sample_rate=sample_rate, channels=channels)
                self.get_logger().info(f"🎙️ Microphone started")
        except Exception as e:
            self.get_logger().error(f"❌ Failed to start microphone: {e}")
            self.mic = None

        try:
            self.client.start()
        except Exception as e:
            self.get_logger().error(f"Failed to connect to OpenAI: {e}")

        # Prepare audio transformer
        target_sr = int(self.get_parameter('target_sample_rate').get_parameter_value().integer_value)
        target_ch = int(self.get_parameter('target_channels').get_parameter_value().integer_value)
        force_transform = bool(self.get_parameter('force_resample_downmix').get_parameter_value().bool_value)
        self._force_transform = force_transform
        self.transformer = AudioTransformer(self.get_logger(), in_channels=self.mic.channels, in_rate=self.mic.sample_rate, out_channels=target_ch, out_rate=target_sr)

        # Ducking state (suppress capture while robot speaks)
        self._duck_until_ts = 0.0
        self._last_robot_text = ""

        def estimate_duck_seconds(text: str) -> float:
            # Approximate TTS duration: ~12 chars/sec with 0.5s tail
            secs = max(1.0, min(8.0, (len(text) / 12.0) + 0.5))
            return secs

        def on_chat_out_duck(msg: String):
            try:
                data = json.loads(msg.data)
            except Exception:
                return
            sender = data.get('sender')
            text = data.get('text', '') or ''
            if sender == 'robot' and text:
                self._last_robot_text = text.strip()
                self._duck_until_ts = time.time() + estimate_duck_seconds(self._last_robot_text)
                self.get_logger().info(f"Ducking mic for robot TTS ~{self._duck_until_ts - time.time():.1f}s")

        # Replace existing chat_out subscription with ducking-aware handler
        try:
            if hasattr(self, 'chat_out_sub') and self.chat_out_sub:
                # NOTE: ROS2 doesn't allow reassigning callback; create another sub to same topic
                pass
        except Exception:
            pass
        self.chat_out_sub = self.create_subscription(String, '/chat_out', on_chat_out_duck, 10)

        def is_ducking_active() -> bool:
            return time.time() < self._duck_until_ts

        # Background audio pump
        self.stop_evt = threading.Event()
        
        if self.mic is None:
            self.get_logger().error("❌ Cannot start audio streaming - microphone failed to initialize")
            return
        
        def audio_loop():
            if not self.client.wait_until_connected(timeout=10):
                self.get_logger().error("WebSocket didn't open in time")
                return
            while not self.stop_evt.is_set():
                try:
                    chunk = self.mic.queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    # Skip sending while ducking
                    if is_ducking_active():
                        continue
                    # Optionally transform to target format (mono 24k)
                    out_bytes = self.transformer.transform(chunk) if self._force_transform else chunk
                    payload = {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(out_bytes).decode("ascii"),
                    }
                    self.client.send_json(payload)
                except Exception as e:
                    self.get_logger().error(f"audio loop error: {e}")

        self.audio_thread = threading.Thread(target=audio_loop, daemon=True)
        self.audio_thread.start()

        # Periodic commit (disabled by default - server VAD handles segmentation)
        commit_interval = float(self.get_parameter('commit_interval_s').get_parameter_value().double_value if hasattr(self.get_parameter('commit_interval_s').get_parameter_value(), 'double_value') else self.get_parameter('commit_interval_s').value)
        if commit_interval > 0:
            self.commit_thread = threading.Thread(target=periodic_commit, args=(self.client, self.stop_evt, self.get_logger(), commit_interval, lambda: not is_ducking_active() and self._speech_active), daemon=True)
            self.commit_thread.start()
        else:
            self.commit_thread = None

        # Stats timer removed to reduce log verbosity

    def destroy_node(self):
        # Stop timers and threads
        try:
            if hasattr(self, 'stats_timer') and self.stats_timer and not self.stats_timer.is_canceled():
                self.stats_timer.cancel()
        except Exception:
            pass
        try:
            self.stop_evt.set()
            if hasattr(self, 'audio_thread'):
                self.audio_thread.join(timeout=1.0)
            if hasattr(self, 'commit_thread') and self.commit_thread is not None:
                self.commit_thread.join(timeout=1.0)
        except Exception:
            pass
        # Stop input backend
        try:
            if self.mic:
                self.mic.stop()
        except Exception:
            pass
        # Stop WS client
        try:
            self.client.stop()
        except Exception:
            pass
        return super().destroy_node()

    def chat_out_callback(self, msg: String):
        # Placeholder: can be used for ducking
        try:
            data = json.loads(msg.data)
            sender = data.get('sender')
            text = data.get('text', '')
            if sender == 'robot' and text:
                self.get_logger().debug('Robot speaking (chat_out), ducking placeholder')
        except Exception:
            pass

    def publish_chat_in(self, text: str):
        out = String()
        out.data = text
        self.chat_in_pub.publish(out)
        self.get_logger().info(f"/chat_in: {text}")

    def handle_set_active(self, request, response):
        self.active = bool(request.data)
        response.success = True
        response.message = 'voice active' if self.active else 'voice inactive'
        return response


def main(args=None):
    rclpy.init(args=args)
    node = VoiceClientNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
