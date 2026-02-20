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
    Synchronous wrapper for async WebSocket connection.
    Provides websocket-client compatible interface.
    """

    def __init__(
        self,
        proxy_url: str,
        get_token: Callable[[], str],
        model: str,
        on_message: Optional[callable] = None,
        on_open: Optional[callable] = None,
        on_error: Optional[callable] = None,
        on_close: Optional[callable] = None,
    ):
        self._proxy_url = proxy_url
        self._get_token = get_token
        self._model = model
        self._on_message = on_message
        self._on_open = on_open
        self._on_error = on_error
        self._on_close = on_close

        self._ws_connection = None
        self._loop = None
        self._loop_thread = None
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._connected = threading.Event()

    def start(self):
        """Start the WebSocket connection in a background thread."""

        def run_async():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                # Build WebSocket URL
                proxy_url = self._proxy_url
                # Ensure URL has a scheme
                if not proxy_url.startswith(("http://", "https://", "ws://", "wss://")):
                    proxy_url = f"https://{proxy_url}"
                # Convert to WebSocket scheme
                ws_url = proxy_url.replace("https://", "wss://").replace(
                    "http://", "ws://"
                )
                token = self._get_token()
                ws_url = f"{ws_url}/v1/services/openai/v1/realtime?model={self._model}&token={token}"

                # Connect to proxy WebSocket
                async def connect():
                    self._ws_connection = await websockets.connect(
                        ws_url,
                        extra_headers={
                            "Authorization": f"Bearer {token}",
                        },
                    )
                    self._connected.set()
                    if self._on_open:
                        self._on_open()

                    # Message receiving loop
                    try:
                        async for message in self._ws_connection:
                            if self._stop.is_set():
                                break
                            if self._on_message:
                                self._on_message(None, message)
                    except Exception as e:
                        if self._on_error:
                            self._on_error(e)
                        else:
                            logger.error(f"[ws receive error] {e}")
                    finally:
                        self._connected.clear()
                        if self._on_close:
                            self._on_close()

                self._loop.run_until_complete(connect())
            except Exception as e:
                if self._on_error:
                    self._on_error(e)
                else:
                    logger.error(f"[proxy ws error] {e}")
                self._connected.clear()
            finally:
                if self._loop:
                    self._loop.close()

        self._loop_thread = threading.Thread(target=run_async, daemon=True)
        self._loop_thread.start()

    def stop(self):
        """Stop the WebSocket connection."""
        self._stop.set()
        if self._ws_connection and self._loop:
            try:
                # Schedule close in the event loop
                asyncio.run_coroutine_threadsafe(
                    self._ws_connection.close(), self._loop
                )
            except Exception:
                pass
        self._connected.clear()

    def wait_until_connected(self, timeout: float = 10.0) -> bool:
        """Wait for connection to be established."""
        return self._connected.wait(timeout=timeout)

    def send_json(self, payload: dict):
        """Send JSON payload via WebSocket."""
        if not self._ws_connection or not self._loop or not self._connected.is_set():
            return

        data = json.dumps(payload)
        with self._send_lock:
            try:
                # Schedule send in the event loop
                asyncio.run_coroutine_threadsafe(
                    self._ws_connection.send(data), self._loop
                )
            except Exception as e:
                if self._on_error:
                    self._on_error(e)
                else:
                    logger.error(f"[send_json] {e}")


class ProxyOpenAIClient:
    """
    OpenAI client that proxies through the innate service proxy.

    Supports both HTTP (Chat Completions) and WebSocket (Realtime) APIs.
    """

    def __init__(
        self,
        proxy_url: Optional[str] = None,
        innate_service_key: Optional[str] = None,
        auth_issuer_url: Optional[str] = None,
    ):
        """
        Initialize OpenAI proxy client.

        Args:
            proxy_url: Proxy service URL
            innate_service_key: Service key for JWT exchange
            auth_issuer_url: innate-auth issuer URL for OIDC discovery
        """
        import os

        self._proxy_url = (proxy_url or os.getenv("INNATE_PROXY_URL", "")).rstrip("/")
        if not self._proxy_url.startswith(("http://", "https://")):
            self._proxy_url = f"https://{self._proxy_url}"
        self._proxy_client = ProxyClient(
            proxy_url=proxy_url,
            innate_service_key=innate_service_key,
            auth_issuer_url=auth_issuer_url,
        )

    def _get_token(self) -> str:
        """Return the current Bearer token (JWT or raw key)."""
        if self._proxy_client._auth is not None:
            return self._proxy_client._auth.token
        return self._proxy_client._service_key

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

        def __init__(self, proxy_url: str, get_token: Callable[[], str]):
            """Initialize Realtime API."""
            self._proxy_url = proxy_url.rstrip("/")
            self._get_token = get_token

        async def connect(
            self,
            model: str = "gpt-4o-realtime-preview",
            on_message: Optional[callable] = None,
        ) -> websockets.WebSocketServerProtocol:
            """
            Connect to OpenAI Realtime API via proxy (async).

            Args:
                model: Model name
                on_message: Callback for received messages

            Returns:
                WebSocket connection
            """
            # Build WebSocket URL
            proxy_url = self._proxy_url
            # Ensure URL has a scheme
            if not proxy_url.startswith(("http://", "https://", "ws://", "wss://")):
                proxy_url = f"https://{proxy_url}"
            # Convert to WebSocket scheme
            ws_url = proxy_url.replace("https://", "wss://").replace("http://", "ws://")
            token = self._get_token()
            ws_url = (
                f"{ws_url}/v1/services/openai/v1/realtime?model={model}&token={token}"
            )

            # Connect to proxy WebSocket
            ws = await websockets.connect(
                ws_url,
                extra_headers={
                    "Authorization": f"Bearer {token}",
                },
            )

            # Set up message handler
            if on_message:

                async def message_handler():
                    async for message in ws:
                        await on_message(ws, message)

                asyncio.create_task(message_handler())

            return ws

        def connect_sync(
            self,
            model: str = "gpt-4o-realtime-preview",
            on_message: Optional[callable] = None,
            on_open: Optional[callable] = None,
            on_error: Optional[callable] = None,
            on_close: Optional[callable] = None,
        ) -> "SyncRealtimeConnection":
            """
            Connect to OpenAI Realtime API via proxy (synchronous interface).

            This provides a synchronous interface compatible with websocket-client library,
            bridging the async websockets library internally.

            Args:
                model: Model name
                on_message: Callback for received messages (sync: (ws, message) -> None)
                on_open: Callback when connection opens (sync: () -> None)
                on_error: Callback for errors (sync: (error) -> None)
                on_close: Callback when connection closes (sync: () -> None)

            Returns:
                SyncRealtimeConnection object with start(), stop(), send_json(), wait_until_connected() methods
            """
            return SyncRealtimeConnection(
                proxy_url=self._proxy_url,
                get_token=self._get_token,
                model=model,
                on_message=on_message,
                on_open=on_open,
                on_error=on_error,
                on_close=on_close,
            )

    @property
    def chat(self) -> Chat:
        """Get Chat API."""
        return self.Chat(self._proxy_client)

    @property
    def realtime(self) -> Realtime:
        """Get Realtime API."""
        return self.Realtime(self._proxy_url, self._get_token)

    async def close(self):
        """Close the client."""
        await self._proxy_client.close()

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()
