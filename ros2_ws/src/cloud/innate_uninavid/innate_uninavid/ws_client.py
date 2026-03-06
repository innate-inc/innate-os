#!/usr/bin/env python3
"""
Websocket client for UniNavid — runs entirely in its own daemon thread.

Exposes a thread-safe API that the ROS node polls via timers:

    client = UninavidWsClient(url, auth_provider, logger)
    client.connect(instruction)   # non-blocking, starts the ws thread
    client.push_frame(data)       # feed a compressed image
    action = client.pop_action()  # get the latest action code (or None)
    client.disconnect()           # tear down the connection
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import deque
from enum import IntEnum, auto
from typing import Optional

import websockets

# ── Public constants ──────────────────────────────────────────────────────────

IMAGE_SEND_HZ: float = 5.0
CONSECUTIVE_STOPS_TO_COMPLETE: int = 20


class Action(IntEnum):
    STOP = 0
    FORWARD = 1
    LEFT = 2
    RIGHT = 3


class ClientState(IntEnum):
    IDLE = auto()
    CONNECTING = auto()
    CONNECTED = auto()
    COMPLETED = auto()      # goal done (consecutive STOPs)
    FAILED = auto()         # connection error
    DISCONNECTED = auto()   # clean shutdown


# ── Client ────────────────────────────────────────────────────────────────────

class UninavidWsClient:
    """Async-websockets client wrapped in a daemon thread.

    All public methods are safe to call from any thread.
    """

    def __init__(
        self,
        url: str,
        auth_provider=None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._url = url
        self._auth = auth_provider
        self._log = logger or logging.getLogger(__name__)

        # ── Thread-safe shared state ──────────────────────────────────────
        self._lock = threading.Lock()
        self._state: ClientState = ClientState.IDLE
        self._action_queue: deque[int] = deque()
        self._consecutive_stops: int = 0
        self._frame: Optional[bytes] = None     # latest outgoing frame
        self._error_msg: Optional[str] = None

        # ── Internals (only touched from the ws thread) ──────────────────
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._instruction: Optional[str] = None

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def state(self) -> ClientState:
        with self._lock:
            return self._state

    @property
    def error_message(self) -> Optional[str]:
        with self._lock:
            return self._error_msg

    @property
    def consecutive_stops(self) -> int:
        with self._lock:
            return self._consecutive_stops

    def connect(self, instruction: str) -> None:
        """Start the websocket connection in a background thread.

        Non-blocking.  Sets state to CONNECTING immediately.
        """
        with self._lock:
            self._state = ClientState.CONNECTING
            self._action_queue.clear()
            self._consecutive_stops = 0
            self._frame = None
            self._error_msg = None
        self._instruction = f"SET_INSTRUCTION:{instruction}"

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="uninavid_ws"
        )
        self._thread.start()

    def disconnect(self) -> None:
        """Request a clean shutdown.  Non-blocking."""
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        with self._lock:
            if self._state not in (
                ClientState.COMPLETED,
                ClientState.FAILED,
            ):
                self._state = ClientState.DISCONNECTED

    def push_frame(self, format: str, stamp_sec: int, stamp_nanosec: int,
                   data: bytes) -> None:
        """Store a compressed image frame for the send loop to pick up."""
        header = json.dumps({
            "type": "image",
            "format": format,
            "stamp_sec": stamp_sec,
            "stamp_nanosec": stamp_nanosec,
        }).encode()
        with self._lock:
            self._frame = header + b"\n" + data

    def pop_action(self) -> Optional[int]:
        """Pop the oldest queued action code, or None if empty."""
        with self._lock:
            if self._action_queue:
                return self._action_queue.popleft()
            return None

    # ── Internal (ws thread) ──────────────────────────────────────────────────

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._session())
        except RuntimeError as exc:
            # loop.stop() from disconnect() causes this — not a real crash
            with self._lock:
                if self._state in (ClientState.DISCONNECTED, ClientState.COMPLETED):
                    self._log.info(f"WS thread stopped cleanly ({exc})")
                else:
                    self._log.error(f"WS thread crashed: {exc}")
                    self._state = ClientState.FAILED
                    self._error_msg = str(exc)
        except Exception as exc:
            self._log.error(f"WS thread crashed: {exc}")
            with self._lock:
                self._state = ClientState.FAILED
                self._error_msg = str(exc)

    async def _session(self) -> None:
        """Single connection attempt — no retry."""
        headers = {}
        if self._auth is not None:
            headers["Authorization"] = f"Bearer {self._auth.token}"

        try:
            async with websockets.connect(
                self._url, additional_headers=headers
            ) as ws:
                with self._lock:
                    self._state = ClientState.CONNECTED
                self._log.info("WS connected")

                # Send instruction
                await ws.send(self._instruction)
                self._log.info(f"Sent: {self._instruction!r}")

                send_task = asyncio.create_task(self._send_loop(ws))
                recv_task = asyncio.create_task(self._recv_loop(ws))

                done, pending = await asyncio.wait(
                    {send_task, recv_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                # Propagate exceptions
                for t in done:
                    if t.exception() is not None:
                        raise t.exception()

        except websockets.ConnectionClosed as exc:
            reason = f"Connection closed (code={exc.code}, reason={exc.reason!r})"
            self._log.info(reason)
            with self._lock:
                if self._state == ClientState.CONNECTED:
                    self._state = ClientState.DISCONNECTED
                    self._error_msg = reason
        except OSError as exc:
            self._log.error(f"WS connection failed: {exc}")
            with self._lock:
                self._state = ClientState.FAILED
                self._error_msg = str(exc)
        except Exception as exc:
            self._log.error(f"WS unexpected error: {exc}")
            with self._lock:
                self._state = ClientState.FAILED
                self._error_msg = str(exc)

    async def _send_loop(self, ws) -> None:
        interval = 1.0 / IMAGE_SEND_HZ
        while True:
            await asyncio.sleep(interval)
            with self._lock:
                payload = self._frame
            if payload is not None:
                await ws.send(payload)

    async def _recv_loop(self, ws) -> None:
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode(errors="replace")

            # Each message is a comma-separated list of action ints
            actions: list[int] = []
            for part in raw.strip().split(","):
                try:
                    actions.append(int(part.strip()))
                except (ValueError, AttributeError):
                    continue
            if not actions:
                continue

            with self._lock:
                for action in actions:
                    self._action_queue.append(action)
                    if action == Action.STOP:
                        self._consecutive_stops += 1
                    else:
                        self._consecutive_stops = 0

                if self._consecutive_stops >= CONSECUTIVE_STOPS_TO_COMPLETE:
                    self._consecutive_stops = 0
                    self._state = ClientState.COMPLETED

            # If completed, notify server and close
            if self.state == ClientState.COMPLETED:
                self._log.info(
                    f"{CONSECUTIVE_STOPS_TO_COMPLETE} consecutive STOPs — done"
                )
                try:
                    await ws.send("SET_INSTRUCTION:null")
                    await ws.close()
                except Exception:
                    pass
                return
