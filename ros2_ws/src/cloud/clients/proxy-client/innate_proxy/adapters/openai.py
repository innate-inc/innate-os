"""OpenAI adapter for the Innate proxy client.

Provides :class:`ProxyOpenAIClient` with a ``realtime`` WebSocket sub-API.

The adapter expects a *parent* object that exposes:

- ``parent.proxy_url``  — base URL of the proxy

An :class:`auth_client.AuthProvider` is passed separately at
construction time for WebSocket auth.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Callable, Optional

from auth_client import AuthProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sync adapter — bridges the async websocket for callers that need
# a threaded callback-based interface (e.g. MicroInput).
#
# TODO: This adds indirection.  Long-term we want to make MicroInput
#       fully async and drop this class entirely.
# ---------------------------------------------------------------------------


class SyncRealtimeConnection:
    """Sync wrapper around an async WebSocket.

    Presents the callback-based API expected by existing consumers::

        conn = proxy.openai.realtime.connect_sync(
            model=model,
            on_message=my_handler,   # (ws, message_str)
            on_open=on_open_cb,      # ()
            on_error=on_error_cb,    # (error)
            on_close=on_close_cb,    # ()
        )
        conn.start()                 # non-blocking, spawns background thread
        conn.wait_until_connected()  # blocks
        conn.send_json({...})
        conn.stop()
    """

    def __init__(
        self,
        auth: AuthProvider,
        ws_url: str,
        on_message: Optional[Callable] = None,
        on_open: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
        on_close: Optional[Callable] = None,
    ) -> None:
        self._auth = auth
        self._ws_url = ws_url
        self._on_message = on_message
        self._on_open = on_open
        self._on_error = on_error
        self._on_close = on_close

        self._ws: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._connected = threading.Event()
        self._stopped = threading.Event()

    # -- public API -----------------------------------------------------------

    def start(self) -> None:
        """Start the background event-loop thread and connect."""
        self._loop = asyncio.new_event_loop()

        def _run() -> None:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._run_ws())

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Close the websocket and stop the background loop."""
        self._stopped.set()
        if self._ws and self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=2.0)
        self._loop = None
        self._thread = None

    def send_json(self, data: dict) -> None:
        """Send a JSON payload (thread-safe)."""
        if self._ws and self._loop and self._loop.is_running():
            raw = json.dumps(data)
            asyncio.run_coroutine_threadsafe(self._ws.send(raw), self._loop)

    def wait_until_connected(self, timeout: float = 10) -> bool:
        """Block until the websocket is open. Returns *True* on success."""
        return self._connected.wait(timeout=timeout)

    # -- internals ------------------------------------------------------------

    async def _run_ws(self) -> None:
        try:
            self._ws = await self._auth.ws_connect(self._ws_url)
            self._connected.set()
            if self._on_open:
                self._on_open()

            async for message in self._ws:
                if self._stopped.is_set():
                    break
                if self._on_message:
                    self._on_message(self._ws, message)
        except Exception as exc:
            if self._on_error and not self._stopped.is_set():
                self._on_error(exc)
        finally:
            self._connected.clear()
            if self._on_close and not self._stopped.is_set():
                self._on_close()

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

    Provides a realtime WebSocket sub-API (async *and* sync).
    Auth is delegated to :mod:`auth_client` — this adapter carries no
    token / JWT logic of its own.
    """

    def __init__(self, parent: Any, auth: AuthProvider | None = None) -> None:
        self._parent = parent
        self._auth = auth

    # -- helpers --------------------------------------------------------------

    def _get_proxy_url(self) -> str:
        return getattr(self._parent, "proxy_url", "")

    # -- Realtime -------------------------------------------------------------

    class Realtime:
        def __init__(self, openai_client: "ProxyOpenAIClient") -> None:
            self._oc = openai_client

        def _build_ws_url(self, model: str) -> str:
            proxy_url = self._oc._get_proxy_url()
            ws_url = proxy_url.replace("https://", "wss://").replace("http://", "ws://")
            return f"{ws_url}/v1/services/openai/v1/realtime?model={model}"

        async def connect(
            self,
            model: str = "gpt-4o-realtime-preview",
            on_message: Optional[Callable] = None,
        ):
            """Open an async WebSocket to the OpenAI Realtime API via proxy."""
            ws_url = self._build_ws_url(model)
            ws = await self._oc._auth.ws_connect(ws_url)

            if on_message:

                async def _handler() -> None:
                    async for message in ws:
                        await on_message(ws, message)

                asyncio.create_task(_handler())

            return ws

        def connect_sync(
            self,
            model: str = "gpt-4o-realtime-preview",
            on_message: Optional[Callable] = None,
            on_open: Optional[Callable] = None,
            on_error: Optional[Callable] = None,
            on_close: Optional[Callable] = None,
        ) -> SyncRealtimeConnection:
            """Return a :class:`SyncRealtimeConnection` (call ``.start()`` to connect)."""
            ws_url = self._build_ws_url(model)
            return SyncRealtimeConnection(
                auth=self._oc._auth,
                ws_url=ws_url,
                on_message=on_message,
                on_open=on_open,
                on_error=on_error,
                on_close=on_close,
            )

    # -- properties -----------------------------------------------------------

    @property
    def realtime(self) -> Realtime:
        return self.Realtime(self)

    async def close(self) -> None:
        await self._parent.close_async()

    async def __aenter__(self) -> "ProxyOpenAIClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
