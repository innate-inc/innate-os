"""OpenAI adapter for the Innate proxy client.

Provides :class:`ProxyOpenAIClient` with ``chat`` (HTTP) and ``realtime``
(WebSocket) sub-APIs, plus :class:`SyncRealtimeConnection` for low-latency
audio streaming from synchronous code.

The adapter expects a *parent* object that exposes:

- ``parent.proxy_url``  — base URL of the proxy
- ``parent.request(...)`` — for HTTP calls

An optional :class:`auth_client.AuthProvider` is passed separately at
construction time for WebSocket auth.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, AsyncIterator, Callable, Dict, Optional

import websocket  # websocket-client (sync, fast)
from auth_client import AuthProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sync WebSocket wrapper  (websocket-client — no httpx/asyncio)
# ---------------------------------------------------------------------------


class SyncRealtimeConnection:
    """Synchronous WebSocket connection for OpenAI Realtime API via proxy.

    Uses ``websocket-client`` (sync) for low-latency audio streaming.
    Auth is handled entirely by the supplied :class:`AuthProvider`.
    """

    def __init__(
        self,
        proxy_url: str,
        model: str = "gpt-4o-realtime-preview",
        auth: Optional[AuthProvider] = None,
        on_message: Optional[Callable] = None,
        on_open: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        on_close: Optional[Callable] = None,
    ) -> None:
        self._proxy_url = proxy_url.rstrip("/")
        self._auth = auth
        self._model = model
        self._on_message_callback = on_message
        self._on_open_callback = on_open
        self._on_error_callback = on_error
        self._on_close_callback = on_close

        self._ws: Optional[websocket.WebSocket] = None
        self._stop_event = threading.Event()
        self._connected_event = threading.Event()
        self._send_lock = threading.Lock()
        self._audio_chunk_count = 0

    def start(self) -> None:
        """Start the WebSocket connection in a background thread.

        Auth (including 401 retry) is delegated to
        :meth:`AuthProvider.ws_connect_sync`.
        """
        self._stop_event.clear()
        self._connected_event.clear()
        self._audio_chunk_count = 0

        ws_url = self._proxy_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        ws_url = f"{ws_url}/v1/services/openai/v1/realtime?model={self._model}"

        logger.info("Connecting to WebSocket: %s...", ws_url[:80])

        self._ws = self._auth.ws_connect_sync(
            ws_url,
            extra_headers={"OpenAI-Beta": "realtime=v1"},
            enable_multithread=True,
        )

        logger.info("WebSocket connected successfully")
        self._connected_event.set()
        if self._on_open_callback:
            self._on_open_callback()

        # Background threads for recv loop and keepalive pings
        threading.Thread(target=self._recv_loop, daemon=True).start()
        threading.Thread(target=self._ping_loop, daemon=True).start()

    def stop(self) -> None:
        """Stop the WebSocket connection."""
        self._stop_event.set()
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
        self._connected_event.clear()

    def wait_until_connected(self, timeout: float = 10.0) -> bool:
        return self._connected_event.wait(timeout=timeout)

    def send_json(self, data: Dict[str, Any]) -> None:
        msg_type = data.get("type", "unknown")
        if msg_type == "input_audio_buffer.append":
            self._audio_chunk_count += 1
            if self._audio_chunk_count == 1:
                logger.info("📤 First audio chunk sent")
            elif self._audio_chunk_count == 100:
                logger.info("📤 100 audio chunks sent")
        else:
            logger.info("📤 send_json: %s", msg_type)

        msg = json.dumps(data)
        with self._send_lock:
            if self._ws and self._connected_event.is_set():
                try:
                    self._ws.send(msg)
                except Exception as e:
                    logger.error("Send error: %s", e)

    # -- internal loops / callbacks -------------------------------------------

    def _recv_loop(self) -> None:
        """Receive messages until stop or disconnect."""
        try:
            while not self._stop_event.is_set():
                try:
                    message = self._ws.recv()
                    if not message:
                        break
                    if isinstance(message, str):
                        self._dispatch_message(message)
                except websocket.WebSocketConnectionClosedException:
                    break
                except Exception as exc:
                    if not self._stop_event.is_set():
                        logger.error("WebSocket error: %s", exc)
                        if self._on_error_callback:
                            self._on_error_callback(str(exc))
                    break
        finally:
            logger.info("WebSocket closed")
            self._connected_event.clear()
            if self._on_close_callback:
                self._on_close_callback()

    def _ping_loop(self) -> None:
        """Send keepalive pings every 30 seconds."""
        while not self._stop_event.wait(timeout=30):
            try:
                if self._ws and self._connected_event.is_set():
                    self._ws.ping()
            except Exception:
                break

    def _dispatch_message(self, message: str) -> None:
        """Log and forward a received text message."""
        try:
            msg_data = json.loads(message)
            msg_type = msg_data.get("type", "unknown")
            if msg_type in (
                "session.created",
                "session.updated",
                "input_audio_buffer.speech_started",
                "input_audio_buffer.speech_stopped",
                "conversation.item.input_audio_transcription.completed",
            ):
                logger.info("📨 %s", msg_type)
            elif msg_type == "error":
                logger.error("📨 OpenAI error: %s", msg_data)
        except Exception:
            pass
        if self._on_message_callback:
            try:
                self._on_message_callback(self._ws, message)
            except Exception as e:
                logger.error("Message handler error: %s", e)

    def _on_error(self, ws: Any, error: Any) -> None:
        logger.error("WebSocket error: %s", error)
        if self._on_error_callback:
            self._on_error_callback(str(error))
        self.stop()

    def _on_close(self, ws: Any, status_code: Any, msg: Any) -> None:
        logger.info("WebSocket closed")
        self._connected_event.clear()
        if self._on_close_callback:
            self._on_close_callback()


# ---------------------------------------------------------------------------
# Main adapter
# ---------------------------------------------------------------------------


class ProxyOpenAIClient:
    """OpenAI client that routes through the Innate service proxy.

    Supports HTTP (Chat Completions) and WebSocket (Realtime) sub-APIs.
    Auth is delegated to :mod:`auth_client` — this adapter carries no
    token / JWT logic of its own.
    """

    def __init__(self, parent: Any, auth: AuthProvider | None = None) -> None:
        self._parent = parent
        self._auth = auth

    # -- helpers --------------------------------------------------------------

    def _get_proxy_url(self) -> str:
        return getattr(self._parent, "proxy_url", "")

    # -- Chat -----------------------------------------------------------------

    class Chat:
        def __init__(self, parent: Any) -> None:
            self._parent = parent

        async def completions(
            self,
            model: str,
            messages: list[Dict[str, str]],
            **kwargs: Any,
        ) -> Dict[str, Any] | AsyncIterator[Dict[str, Any]]:
            body: Dict[str, Any] = {"model": model, "messages": messages, **kwargs}
            if stream:
                body["stream"] = True

            response = await self._parent.request_async(
                service_name="openai",
                endpoint="/v1/chat/completions",
                method="POST",
                json=body,
            )

            if stream:

                async def _stream() -> AsyncIterator[Dict[str, Any]]:
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data = line[6:]
                            if data.strip() == "[DONE]":
                                break
                            try:
                                yield json.loads(data)
                            except json.JSONDecodeError:
                                continue

                return _stream()
            return response.json()

    # -- Realtime -------------------------------------------------------------

    class Realtime:
        def __init__(self, openai_client: "ProxyOpenAIClient") -> None:
            self._oc = openai_client

        def connect_sync(
            self,
            model: str = "gpt-4o-realtime-preview",
            on_message: Optional[Callable] = None,
            on_open: Optional[Callable] = None,
            on_error: Optional[Callable] = None,
            on_close: Optional[Callable] = None,
        ) -> SyncRealtimeConnection:
            return SyncRealtimeConnection(
                proxy_url=self._oc._get_proxy_url(),
                model=model,
                auth=self._oc._auth,
                on_message=on_message,
                on_open=on_open,
                on_error=on_error,
                on_close=on_close,
            )

        async def connect(
            self,
            model: str = "gpt-4o-realtime-preview",
            on_message: Optional[Callable] = None,
        ):
            """Open an async WebSocket to the OpenAI Realtime API via proxy."""
            proxy_url = self._oc._get_proxy_url()
            ws_url = proxy_url.replace("https://", "wss://").replace("http://", "ws://")
            ws_url = f"{ws_url}/v1/services/openai/v1/realtime?model={model}"

            ws = await self._oc._auth.ws_connect(ws_url)

            if on_message:

                async def _handler() -> None:
                    async for message in ws:
                        await on_message(ws, message)

                asyncio.create_task(_handler())

            return ws

    # -- properties -----------------------------------------------------------

    @property
    def chat(self) -> Chat:
        return self.Chat(self._parent)

    @property
    def realtime(self) -> Realtime:
        return self.Realtime(self)

    async def close(self) -> None:
        await self._parent.close_async()

    async def __aenter__(self) -> "ProxyOpenAIClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
