# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The one I/O loop, written once: check → body → wire → events.

An :class:`Adapter` is two pure functions and a table of capabilities; a
:class:`Provider` is an adapter bound to an :class:`Http` mover and a model
name. ``stream`` is the only primitive — ``run`` is the stream folded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from brain_client.llm.types import (
    Capabilities,
    Event,
    Json,
    LlmError,
    Message,
    Model,
    Reply,
    Request,
    TextDelta,
    ThoughtDelta,
    Wire,
)

if TYPE_CHECKING:
    from brain_client.llm.http import Http

Callback = Callable[[str], None] | None


class Adapter(Protocol):
    @property
    def wire(self) -> Wire: ...

    @property
    def path(self) -> str: ...  # the request path, "{model}" substituted by the provider

    @property
    def caps(self) -> Capabilities: ...

    def body(self, request: Request, model: Model) -> Json: ...

    def events(self, lines: Iterator[str]) -> Iterator[Event]: ...


@runtime_checkable
class Pinned(Protocol):
    """An explicit server-side context cache — only Gemini has words for it."""

    def pin(self, system: str, messages: Sequence[Message], *, ttl_s: int, display_name: str = "") -> str: ...

    def unpin(self, handle: str) -> None: ...


def fold(events: Iterator[Event], on_text: Callback = None, on_thought: Callback = None) -> Reply:
    """Drain a stream into its Reply, handing deltas to the callbacks as they arrive."""
    for event in events:
        if isinstance(event, Reply):
            return event
        if isinstance(event, TextDelta):
            if on_text is not None:
                on_text(event.text)
        elif isinstance(event, ThoughtDelta) and on_thought is not None:
            on_thought(event.text)
    raise LlmError.protocol("stream ended without a reply")


@dataclass(frozen=True)
class Provider:
    adapter: Adapter
    http: Http
    model: Model
    extra_body: Json = field(default_factory=dict)  # merged last: a server's own knobs, never an adapter's job

    def stream(self, request: Request, *, timeout: float | None = None) -> Iterator[Event]:
        self.adapter.caps.check(request)
        self.model.check(request)
        body = {**self.adapter.body(request, self.model), **self.extra_body}
        path = self.adapter.path.format(model=self.model.name)
        yield from self.adapter.events(self.http.sse(path, body, timeout=timeout))

    def run(
        self, request: Request, *, on_text: Callback = None, on_thought: Callback = None, timeout: float | None = None
    ) -> Reply:
        return fold(self.stream(request, timeout=timeout), on_text, on_thought)
