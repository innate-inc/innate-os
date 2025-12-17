"""OpenAI adapter for proxy client."""

import asyncio
import logging
import json
import threading
from typing import Optional, Dict, Any, AsyncIterator, Callable
from brain_client.client.proxy_client import ProxyClient
import websocket  # websocket-client library (sync, fast)
import websockets  # For async connect() method

# Configure logging to output to console
logging.basicConfig(level=logging.INFO, format='[openai_adapter] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class SyncRealtimeConnection:
    """
    Synchronous WebSocket connection for OpenAI Realtime API via proxy.
    
    Uses websocket-client (sync library) for low-latency audio streaming.
    """
    
    def __init__(
        self,
        proxy_url: str,
        innate_service_key: str,
        model: str = "gpt-4o-realtime-preview",
        on_message: Optional[Callable] = None,
        on_open: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        on_close: Optional[Callable] = None,
    ):
        self._proxy_url = proxy_url.rstrip("/")
        self._innate_service_key = innate_service_key
        self._model = model
        self._on_message_callback = on_message
        self._on_open_callback = on_open
        self._on_error_callback = on_error
        self._on_close_callback = on_close
        
        self._ws: Optional[websocket.WebSocketApp] = None
        self._stop_event = threading.Event()
        self._connected_event = threading.Event()
        self._send_lock = threading.Lock()
        self._audio_chunk_count = 0
    
    def start(self):
        """Start the WebSocket connection in a background thread."""
        self._stop_event.clear()
        self._connected_event.clear()
        
        # Build WebSocket URL for proxy
        ws_url = self._proxy_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url}/v1/services/openai/v1/realtime?model={self._model}&token={self._innate_service_key}"
        
        logger.info(f"Connecting to WebSocket: {ws_url[:80]}...")
        
        headers = {
            "X-Innate-Token": self._innate_service_key,
            "OpenAI-Beta": "realtime=v1",
        }
        
        self._ws = websocket.WebSocketApp(
            ws_url,
            header=[f"{k}: {v}" for k, v in headers.items()],
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        
        thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 30, "ping_timeout": 10},
            daemon=True,
        )
        thread.start()
    
    def stop(self):
        """Stop the WebSocket connection."""
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._connected_event.clear()
    
    def wait_until_connected(self, timeout: float = 10.0) -> bool:
        """Wait until the connection is established."""
        return self._connected_event.wait(timeout=timeout)
    
    def send_json(self, data: Dict[str, Any]):
        """Send JSON data over the WebSocket (non-blocking)."""
        msg_type = data.get("type", "unknown")
        
        # Minimal logging for audio chunks
        if msg_type == "input_audio_buffer.append":
            self._audio_chunk_count += 1
            if self._audio_chunk_count == 1:
                logger.info("📤 First audio chunk sent")
            elif self._audio_chunk_count == 100:
                logger.info("📤 100 audio chunks sent")
            # Skip logging for other audio chunks
        else:
            logger.info(f"📤 send_json: {msg_type}")
        
        msg = json.dumps(data)
        with self._send_lock:
            if self._ws and self._connected_event.is_set():
                try:
                    self._ws.send(msg)
                except Exception as e:
                    logger.error(f"Send error: {e}")
    
    # --- WebSocket callbacks ---
    def _on_open(self, ws):
        """Called when WebSocket connects."""
        logger.info("WebSocket connected successfully")
        self._connected_event.set()
        if self._on_open_callback:
            self._on_open_callback()
    
    def _on_message(self, ws, message: str):
        """Called when message received."""
        # Log important events only
        try:
            msg_data = json.loads(message)
            msg_type = msg_data.get("type", "unknown")
            if msg_type in ("session.created", "session.updated"):
                logger.info(f"📨 {msg_type}")
            elif msg_type in ("input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped",
                              "conversation.item.input_audio_transcription.completed"):
                logger.info(f"📨 {msg_type}")
            elif msg_type == "error":
                logger.error(f"📨 OpenAI error: {msg_data}")
        except Exception:
            pass
        
        if self._on_message_callback:
            try:
                self._on_message_callback(ws, message)
            except Exception as e:
                logger.error(f"Message handler error: {e}")
    
    def _on_error(self, ws, error):
        """Called on WebSocket error."""
        logger.error(f"WebSocket error: {error}")
        if self._on_error_callback:
            self._on_error_callback(str(error))
    
    def _on_close(self, ws, status_code, msg):
        """Called when WebSocket closes."""
        logger.info("WebSocket closed")
        self._connected_event.clear()
        if self._on_close_callback:
            self._on_close_callback()


class ProxyOpenAIClient:
    """
    OpenAI client that proxies through the innate service proxy.
    
    Supports both HTTP (Chat Completions) and WebSocket (Realtime) APIs.
    """
    
    def __init__(
        self,
        proxy_url: Optional[str] = None,
        innate_service_key: Optional[str] = None,
    ):
        """
        Initialize OpenAI proxy client.
        
        Args:
            proxy_url: Proxy service URL
            innate_service_key: Authentication token
        """
        import os
        self._proxy_url = proxy_url or os.getenv("INNATE_PROXY_URL", "")
        self._innate_service_key = innate_service_key or os.getenv("INNATE_SERVICE_KEY", "")
        self._proxy_client = ProxyClient(proxy_url=proxy_url, innate_service_key=innate_service_key)
    
    class Chat:
        """Chat Completions API."""
        
        def __init__(self, proxy_client: ProxyClient):
            """Initialize Chat API."""
            self._proxy_client = proxy_client
        
        async def completions(
            self,
            model: str,
            messages: list[Dict[str, str]],
            stream: bool = False,
            **kwargs: Any,
        ) -> Dict[str, Any] | AsyncIterator[Dict[str, Any]]:
            """
            Create chat completion.
            
            Args:
                model: Model name (e.g., 'gpt-4')
                messages: List of messages
                stream: Whether to stream the response
                **kwargs: Additional OpenAI API parameters
                
            Returns:
                Response dict or async iterator for streaming
            """
            body = {
                "model": model,
                "messages": messages,
                **kwargs,
            }
            # Only include stream field if True (some APIs reject stream: false)
            if stream:
                body["stream"] = True
            
            response = await self._proxy_client.request(
                service_name="openai",
                endpoint="/v1/chat/completions",
                method="POST",
                json=body,
            )
            
            if stream:
                # Return async iterator for SSE streaming
                async def stream_response():
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]  # Remove "data: " prefix
                            if data.strip() == "[DONE]":
                                break
                            try:
                                yield json.loads(data)
                            except json.JSONDecodeError:
                                continue
                
                return stream_response()
            else:
                return response.json()
    
    class Realtime:
        """Realtime API (WebSocket)."""
        
        def __init__(self, proxy_url: str, innate_service_key: str):
            """Initialize Realtime API."""
            self._proxy_url = proxy_url.rstrip("/")
            self._innate_service_key = innate_service_key
        
        def connect_sync(
            self,
            model: str = "gpt-4o-realtime-preview",
            on_message: Optional[callable] = None,
            on_open: Optional[callable] = None,
            on_error: Optional[callable] = None,
            on_close: Optional[callable] = None,
        ) -> "SyncRealtimeConnection":
            """
            Create a synchronous WebSocket connection to OpenAI Realtime API via proxy.
            
            Returns a SyncRealtimeConnection object with start(), stop(), send_json() methods.
            """
            return SyncRealtimeConnection(
                proxy_url=self._proxy_url,
                innate_service_key=self._innate_service_key,
                model=model,
                on_message=on_message,
                on_open=on_open,
                on_error=on_error,
                on_close=on_close,
            )
        
        async def connect(
            self,
            model: str = "gpt-4o-realtime-preview",
            on_message: Optional[callable] = None,
        ) -> websockets.WebSocketServerProtocol:
            """
            Connect to OpenAI Realtime API via proxy.
            
            Args:
                model: Model name
                on_message: Callback for received messages
                
            Returns:
                WebSocket connection
            """
            # Build WebSocket URL
            ws_url = self._proxy_url.replace("https://", "wss://").replace("http://", "ws://")
            ws_url = f"{ws_url}/v1/services/openai/v1/realtime?model={model}&token={self._innate_service_key}"
            
            # Connect to proxy WebSocket
            headers = {"Authorization": f"Bearer {self._innate_service_key}"}
            try:
                ws = await websockets.connect(ws_url, additional_headers=headers)
            except TypeError:
                ws = await websockets.connect(ws_url, extra_headers=headers)
            
            # Set up message handler
            if on_message:
                async def message_handler():
                    async for message in ws:
                        await on_message(ws, message)
                
                asyncio.create_task(message_handler())
            
            return ws
    
    @property
    def chat(self) -> Chat:
        """Get Chat API."""
        return self.Chat(self._proxy_client)
    
    @property
    def realtime(self) -> Realtime:
        """Get Realtime API."""
        return self.Realtime(self._proxy_url, self._innate_service_key)
    
    async def close(self):
        """Close the client."""
        await self._proxy_client.close()
    
    async def __aenter__(self):
        """Async context manager entry."""
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()

