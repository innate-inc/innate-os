"""Capture Python logging output into a ring buffer for SSE streaming."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any


class LogEntry:
    __slots__ = ("timestamp", "level", "message")

    def __init__(self, timestamp: float, level: str, message: str) -> None:
        self.timestamp = timestamp
        self.level = level
        self.message = message

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "level": self.level,
            "message": self.message,
        }


class _Waiter:
    __slots__ = ("event", "loop")

    def __init__(self, event: asyncio.Event, loop: asyncio.AbstractEventLoop) -> None:
        self.event = event
        self.loop = loop


class LogCapture(logging.Handler):
    """Logging handler that stores entries in a ring buffer and notifies waiters."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self.buffer: deque[LogEntry] = deque(maxlen=capacity)
        self._total_emitted: int = 0
        self._waiters: list[_Waiter] = []

    def emit(self, record: logging.LogRecord) -> None:
        entry = LogEntry(
            timestamp=time.time(),
            level=record.levelname,
            message=self.format(record),
        )
        self.buffer.append(entry)
        self._total_emitted += 1
        for waiter in list(self._waiters):
            try:
                waiter.loop.call_soon_threadsafe(waiter.event.set)
            except RuntimeError:
                pass

    async def subscribe(self) -> Any:
        """Async generator that yields new LogEntry objects as they arrive.

        Tracks position by a monotonic emit counter rather than buffer length,
        so subscribers continue to receive entries after the ring buffer
        reaches its capacity (otherwise len(buffer) plateaus at maxlen and
        the slice would always be empty).
        """
        seen = self._total_emitted
        event = asyncio.Event()
        waiter = _Waiter(event, asyncio.get_running_loop())
        self._waiters.append(waiter)
        try:
            while True:
                # Clear before reading the counter so any emit that fires
                # between here and the next wait() will re-set the event
                # rather than being erased.
                event.clear()
                total = self._total_emitted
                if total > seen:
                    entries = list(self.buffer)
                    new_count = min(total - seen, len(entries))
                    for entry in entries[-new_count:]:
                        yield entry
                    seen = total
                await event.wait()
        finally:
            self._waiters.remove(waiter)


log_capture = LogCapture()
log_capture.setLevel(logging.DEBUG)
log_capture.setFormatter(logging.Formatter("%(name)s: %(message)s"))
