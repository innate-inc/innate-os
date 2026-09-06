"""Text-free acoustic onset and exactly-once utterance completion, per session."""

import threading
import uuid
from collections.abc import Callable


class SpeechLifecycle:
    def __init__(self, emit: Callable[[dict], None], timeout_s: float = 90.0, *, lock=None):
        self._emit = emit
        self._timeout_s = timeout_s
        self._lock = lock if lock is not None else threading.RLock()
        # A producer with its own session state supplies that same RLock, so
        # timeout callbacks cannot invert lifecycle/session lock ordering.
        self._pending: dict[str, threading.Timer] = {}
        self._closed = False

    def start(self) -> str | None:
        with self._lock:
            if self._closed:
                return None
            token = uuid.uuid4().hex
            timer = threading.Timer(self._timeout_s, self.finish, args=(token,), kwargs={"reason": "timeout"})
            timer.daemon = True
            self._pending[token] = timer
            self._emit({"utterance_id": token, "stage": "started"})
            timer.start()
            return token

    def ended(self, token: str | None) -> None:
        with self._lock:
            if token in self._pending:
                self._emit({"utterance_id": token, "stage": "pending"})

    def finish(self, token: str | None, text: str = "", *, reason: str = "completed") -> bool:
        with self._lock:
            timer = self._pending.pop(token, None)
            if timer is None:
                return False
            timer.cancel()
            self._emit({"utterance_id": token, "stage": "finished", "text": text, "reason": reason})
            return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for token in list(self._pending):
                self.finish(token, reason="session_closed")
