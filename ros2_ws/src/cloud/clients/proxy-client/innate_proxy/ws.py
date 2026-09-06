# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Sync wrapper around an async proxy WebSocket.

The transport under the realtime adapters (ElevenLabs Scribe) — the adapter
supplies the URL and the frame vocabulary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable
from typing import Any

from auth_client import AuthProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TODO: This adds indirection.  Long-term we want to make MicroInput
#       fully async and drop this class entirely.
# ---------------------------------------------------------------------------


class SyncRealtimeConnection:
    """Sync wrapper around an async WebSocket.

    Presents the callback-based API expected by existing consumers::

        conn = proxy.elevenlabs.realtime.connect_sync(
            model_id=model,
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
        auth: AuthProvider | None,
        ws_url: str,
        on_message: Callable | None = None,
        on_open: Callable | None = None,
        on_error: Callable | None = None,
        on_close: Callable | None = None,
    ) -> None:
        self._auth = auth
        self._ws_url = ws_url
        self._on_message = on_message
        self._on_open = on_open
        self._on_error = on_error
        self._on_close = on_close

        self._ws: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._stopped = threading.Event()

    # -- public API -----------------------------------------------------------

    def start(self) -> None:
        """Start the background event-loop thread and connect."""
        self._loop = loop = asyncio.new_event_loop()

        def _run() -> None:
            # Bound locally: stop() nulls self._loop, possibly while this
            # thread is still unwinding.
            asyncio.set_event_loop(loop)
            self._task = loop.create_task(self._run_ws())
            try:
                loop.run_until_complete(self._task)
            except asyncio.CancelledError:
                pass  # normal stop() path
            finally:
                loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Close the websocket and stop the background loop."""
        self._stopped.set()
        # Cancel the task and let _run_ws unwind (closing the socket on the
        # way out); stopping the loop out from under run_until_complete would
        # raise "Event loop stopped before Future completed" in the thread.
        if self._task and self._loop and not self._loop.is_closed():
            try:
                self._loop.call_soon_threadsafe(self._task.cancel)
            except RuntimeError:
                pass  # loop closed between the check and the call — already done
        if self._thread:
            self._thread.join(timeout=2.0)
        self._loop = None
        self._task = None
        self._thread = None

    def send_json(self, data: dict) -> bool:
        """Send a JSON payload (thread-safe). False when the socket is down and
        the payload was dropped — callers sending control frames must know."""
        if self._ws and self._loop and self._loop.is_running():
            raw = json.dumps(data)
            asyncio.run_coroutine_threadsafe(self._ws.send(raw), self._loop)
            return True
        return False

    def wait_until_connected(self, timeout: float = 10) -> bool:
        """Block until the websocket is open. Returns *True* on success."""
        return self._connected.wait(timeout=timeout)

    # -- internals ------------------------------------------------------------

    async def _run_ws(self) -> None:
        try:
            if self._auth is not None:
                self._ws = await self._auth.ws_connect(self._ws_url)
            else:
                from websockets import connect

                self._ws = await connect(self._ws_url)
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
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:
                    pass
            if self._on_close and not self._stopped.is_set():
                self._on_close()
