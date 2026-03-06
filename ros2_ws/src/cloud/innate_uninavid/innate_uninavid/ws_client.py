#!/usr/bin/env python3
"""
Websocket client for UniNavid — runs entirely in its own daemon thread.

Uses the ``websockets`` library for websocket transport with automatic
ping/pong handling and 401 token renewal.  Exposes a thread-safe API
that the ROS node polls:

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
import time
from collections import deque
from enum import IntEnum, auto
from typing import Optional

import websockets

# ── Public constants ──────────────────────────────────────────────────────────

IMAGE_SEND_HZ: float = 10.0
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
        logger=None,
    ) -> None:
        self._url = url
        self._auth = auth_provider
        self._log = logger or logging.getLogger(__name__)

        # ── Thread-safe shared state ──────────────────────────────────────
        self._lock = threading.Lock()
        self._state: ClientState = ClientState.IDLE
        self._action_queue: deque[int] = deque(maxlen=4)
        self._consecutive_stops: int = 0
        self._frame: Optional[bytes] = None     # latest outgoing frame
        self._frame_stamp: tuple[int, int] = (0, 0)  # (sec, nsec) of latest frame
        self._error_msg: Optional[str] = None
        self._rtt_samples: deque[float] = deque(maxlen=500)
        self._sent_stamps: deque[tuple[int, int]] = deque(maxlen=200)
        self._last_send_time: float = 0.0

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
        self._instruction = instruction

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
            self._frame_stamp = (stamp_sec, stamp_nanosec)

    def pop_action(self) -> Optional[int]:
        """Pop the oldest queued action code, or None if empty."""
        with self._lock:
            if self._action_queue:
                return self._action_queue.popleft()
            return None

    def pop_rtt_samples(self) -> list[float]:
        """Drain and return all accumulated round-trip-time samples (seconds)."""
        with self._lock:
            samples = list(self._rtt_samples)
            self._rtt_samples.clear()
            return samples

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
        """Single connection attempt with one 401 retry."""
        for attempt in range(2):
            extra_headers = {"User-Agent": "innate-robot"}
            if self._auth is not None:
                extra_headers["Authorization"] = f"Bearer {self._auth.token}"

            try:
                async with websockets.connect(
                    self._url,
                    additional_headers=extra_headers,
                    max_size=4 * 1024 * 1024,
                    ping_interval=20,
                    ping_timeout=20,
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
                    return  # success

            except websockets.InvalidStatus as exc:
                status = exc.response.status_code
                if status == 401 and attempt == 0 and self._auth is not None:
                    self._log.warning("WS auth failed (401) — renewing token and retrying")
                    self._auth.token_needs_renewal = True
                    continue
                self._log.error(f"WS connection rejected: HTTP {status}")
                with self._lock:
                    self._state = ClientState.FAILED
                    self._error_msg = f"HTTP {status}"
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
                self._log.error(f"WS unexpected error: {type(exc).__name__}: {exc}")
                with self._lock:
                    self._state = ClientState.FAILED
                    self._error_msg = str(exc)
            return  # don't retry on non-401 errors

    async def _send_loop(self, ws) -> None:
        interval = 1.0 / IMAGE_SEND_HZ
        _send_count = 0
        while True:
            await asyncio.sleep(interval)
            with self._lock:
                payload = self._frame
                stamp = self._frame_stamp
                self._frame = None  # don't re-send the same frame
            if payload is not None:
                await ws.send(payload)
                _send_count += 1
                now = time.monotonic()
                with self._lock:
                    self._sent_stamps.append(stamp)
                    self._last_send_time = now
                self._log.info(
                    f"ws_send #{_send_count} stamp={stamp[0]}.{stamp[1]:09d} ({len(payload)} bytes)"
                )

    async def _recv_loop(self, ws) -> None:
        _recv_count = 0
        self._log.info("recv_loop: waiting for server messages…")
        while True:
            try:
                raw = await ws.recv()
            except websockets.ConnectionClosed as exc:
                self._log.info(
                    f"recv_loop: server disconnected (code={exc.code}, reason={exc.reason!r})"
                )
                return
            except Exception as exc:
                self._log.error(f"recv_loop: unexpected error: {type(exc).__name__}: {exc}")
                return

            if isinstance(raw, bytes):
                self._log.debug(f"recv_loop: ignoring binary message ({len(raw)} bytes)")
                continue

            _recv_count += 1
            # Each message is a comma-separated list of action ints
            actions: list[int] = []
            for part in raw.strip().split(","):
                try:
                    actions.append(int(part.strip()))
                except (ValueError, AttributeError):
                    continue

            action_labels = []
            for a in actions:
                try:
                    action_labels.append(Action(a).name)
                except ValueError:
                    action_labels.append(str(a))

            # First two ints are the image stamp (sec, nsec); rest are actions
            stamp_sec = actions[0] if len(actions) > 0 else -1
            stamp_nsec = actions[1] if len(actions) > 1 else -1
            actions = actions[2:]
            action_labels = action_labels[2:]

            rtt = time.time() - (stamp_sec + stamp_nsec / 1e9) if stamp_sec > 0 else -1.0
            with self._lock:
                since_send = time.monotonic() - self._last_send_time if self._last_send_time > 0 else -1.0

            # Report dropped frames (sent stamps older than the one the server responded to)
            resp_stamp = (stamp_sec, stamp_nsec)
            dropped = []
            with self._lock:
                while self._sent_stamps and self._sent_stamps[0] != resp_stamp:
                    dropped.append(self._sent_stamps.popleft())
                if self._sent_stamps and self._sent_stamps[0] == resp_stamp:
                    self._sent_stamps.popleft()  # consume the matched one
            if dropped:
                self._log.info(
                    f"dropped {len(dropped)} frames: {[f'{s}.{ns:09d}' for s, ns in dropped]}"
                )

            self._log.info(
                f"recv #{_recv_count}: stamp={stamp_sec}.{stamp_nsec:09d}  {action_labels}  rtt={rtt:.3f}s  since_send={since_send:.3f}s"
            )

            if not actions:
                continue

            with self._lock:
                for action in actions:
                    self._action_queue.append(action)
                    if action == Action.STOP:
                        self._consecutive_stops += 1
                    else:
                        self._consecutive_stops = 0

                if rtt > 0:
                    self._rtt_samples.append(rtt)

                if self._consecutive_stops >= CONSECUTIVE_STOPS_TO_COMPLETE:
                    self._consecutive_stops = 0
                    self._state = ClientState.COMPLETED

            # If completed, notify server and close
            if self.state == ClientState.COMPLETED:
                self._log.info(
                    f"{CONSECUTIVE_STOPS_TO_COMPLETE} consecutive STOPs — done"
                )
                try:
                    await ws.close()
                except Exception:
                    pass
                return
