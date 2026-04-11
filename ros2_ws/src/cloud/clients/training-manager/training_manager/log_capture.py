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
        self._waiters: list[_Waiter] = []

    def emit(self, record: logging.LogRecord) -> None:
        entry = LogEntry(
            timestamp=time.time(),
            level=record.levelname,
            message=self.format(record),
        )
        self.buffer.append(entry)
        for waiter in list(self._waiters):
            try:
                waiter.loop.call_soon_threadsafe(waiter.event.set)
            except RuntimeError:
                pass

    async def subscribe(self) -> Any:
        """Async generator that yields new LogEntry objects as they arrive."""
        seen = len(self.buffer)
        event = asyncio.Event()
        waiter = _Waiter(event, asyncio.get_running_loop())
        self._waiters.append(waiter)
        try:
            while True:
                entries = list(self.buffer)
                new_entries = entries[seen:]
                seen = len(entries)
                for entry in new_entries:
                    yield entry
                event.clear()
                await event.wait()
        finally:
            self._waiters.remove(waiter)


log_capture = LogCapture()
log_capture.setLevel(logging.DEBUG)
log_capture.setFormatter(logging.Formatter("%(name)s: %(message)s"))
