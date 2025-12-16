"""OpenAI adapter for proxy client."""

import logging
import json
import threading
from typing import Optional, Dict, Any, AsyncIterator, Callable
from client.proxy_client import ProxyClient
import websockets
import asyncio

logger = logging.getLogger(__name__)


class SyncRealtimeConnection:
    """
    Synchronous WebSocket connection wrapper for OpenAI Realtime API via proxy.
    
    Bridges async websockets library with sync interface expected by micro_input.py.
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
        self._on_message = on_message
        self._on_open = on_open
        self._on_error = on_error
        self._on_close = on_close
        
        self._ws = None
        self._loop = None
        self._thread = None
        self._stop_event = threading.Event()
        self._connected_event = threading.Event()
        self._send_lock = threading.Lock()
    
    def start(self):
        """Start the WebSocket connection in a background thread."""
        self._stop_event.clear()
        self._connected_event.clear()
        self._thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self._thread.start()
    
    def stop(self):
        """Stop the WebSocket connection."""
        self._stop_event.set()
        if self._loop and self._ws:
            # Schedule close on the event loop
            asyncio.run_coroutine_threadsafe(self._close_ws(), self._loop)
        if self._thread:
            self._thread.join(timeout=5.0)
    
    def wait_until_connected(self, timeout: float = 10.0) -> bool:
        """Wait until the connection is established."""
        return self._connected_event.wait(timeout=timeout)
    
    def send_json(self, data: Dict[str, Any]):
        """Send JSON data over the WebSocket."""
        if not self._ws:
            logger.error("Cannot send: WebSocket not connected")
            return
        if not self._loop:
            logger.error("Cannot send: Event loop not running")
            return
        with self._send_lock:
            try:
                msg = json.dumps(data)
                logger.debug(f"Sending WebSocket message: {msg[:200]}...")
                future = asyncio.run_coroutine_threadsafe(
                    self._ws.send(msg),
                    self._loop
                )
                future.result(timeout=5.0)
                logger.debug("WebSocket message sent successfully")
            except Exception as e:
                logger.error(f"Error sending WebSocket message: {type(e).__name__}: {e}")
    
    async def _close_ws(self):
        """Close the WebSocket connection."""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
    
    def _run_async_loop(self):
        """Run the async event loop in a thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        
        try:
            self._loop.run_until_complete(self._connect_and_listen())
        except Exception as e:
            logger.error(f"Async loop error: {e}")
            if self._on_error:
                self._on_error(str(e))
        finally:
            self._loop.close()
            if self._on_close:
                self._on_close()
    
    async def _connect_and_listen(self):
        """Connect to WebSocket and listen for messages."""
        # Build WebSocket URL
        ws_url = self._proxy_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url}/v1/services/openai/v1/realtime?model={self._model}&token={self._innate_service_key}"
        
        try:
            # Headers for proxy authentication and OpenAI API
            headers = {
                "X-Innate-Token": self._innate_service_key,
                "OpenAI-Beta": "realtime=v1",
            }
            logger.info(f"Connecting to WebSocket: {ws_url[:80]}...")
            try:
                self._ws = await websockets.connect(ws_url, additional_headers=headers)
            except TypeError:
                # Fallback for different websockets versions
                self._ws = await websockets.connect(ws_url, extra_headers=headers)
            logger.info("WebSocket connected successfully")
            
            self._connected_event.set()
            
            if self._on_open:
                self._on_open()
            
            # Listen for messages
            async for message in self._ws:
                if self._stop_event.is_set():
                    break
                if self._on_message:
                    try:
                        self._on_message(self._ws, message)
                    except Exception as e:
                        logger.error(f"Error in message handler: {e}")
                        
        except Exception as e:
            logger.error(f"WebSocket connection error: {e}")
            if self._on_error:
                self._on_error(str(e))


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

