# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""A scripted stand-in for :class:`~innate_llm.provider.Provider`, for tests without a vendor."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence

from innate_llm.models import resolve
from innate_llm.provider import Callback, fold
from innate_llm.types import Event, Model, Reply, Request

Script = Callable[[Request], Iterable[Event]]


class Replay:
    """Answers every request with ``events`` (or what ``script`` returns for it) and records the requests."""

    def __init__(self, events: Sequence[Event] = (), *, script: Script | None = None, model: Model | str = "replay"):
        self._events = tuple(events)
        self._script = script
        self.model = model if isinstance(model, Model) else resolve(model)
        self.requests: list[Request] = []
        self.timeouts: list[float | None] = []

    @property
    def last(self) -> Request:
        return self.requests[-1]

    def stream(self, request: Request, *, timeout: float | None = None) -> Iterator[Event]:
        self.requests.append(request)
        self.timeouts.append(timeout)
        yield from self._script(request) if self._script is not None else self._events

    def run(
        self, request: Request, *, on_text: Callback = None, on_thought: Callback = None, timeout: float | None = None
    ) -> Reply:
        return fold(self.stream(request, timeout=timeout), on_text, on_thought)
