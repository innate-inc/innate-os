# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The vocabulary every vendor is translated to and from.

Everything here is a frozen value. A conversation is a tuple of
:class:`Message` the caller replaces, never edits; a vendor's private state
(thought signatures, signed thinking blocks, encrypted reasoning) rides the
part it belongs to as ``native`` and is replayed only to the wire that
produced it — so dropping a part drops its state on every wire alike.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from innate_llm.enums import StrEnum
from innate_llm.errors import Kind, LlmError

Json = dict[str, Any]
"""A JSON object as a vendor sends or takes it: a wire body, a schema, a call's arguments."""


class Role(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class Wire(StrEnum):
    """A vendor API dialect — what an adapter speaks, and what ``native`` state is scoped to."""

    GEMINI = "gemini"
    ANTHROPIC = "anthropic"
    OPENAI_RESPONSES = "openai_responses"
    OPENAI_CHAT = "openai_chat"


class Vendor(StrEnum):
    """The ``vendor:`` prefix of a model setting — wire-visible in settings.yaml, never renamed."""

    GOOGLE = "google"
    OPENAI = "openai"
    OPENAI_CHAT = "openai-chat"
    ANTHROPIC = "anthropic"


class Thinking(StrEnum):
    """One reasoning-effort ladder for every vendor; adapters clamp to the rungs they have."""

    DEFAULT = ""
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


LADDER: tuple[Thinking, ...] = (
    Thinking.MINIMAL,
    Thinking.LOW,
    Thinking.MEDIUM,
    Thinking.HIGH,
    Thinking.XHIGH,
    Thinking.MAX,
)


class Finish(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    REFUSAL = "refusal"


Native = tuple[Wire, Json]
"""A vendor's own encoding of one part — replayed verbatim on its wire, ignored on every other."""


@dataclass(frozen=True)
class Text:
    text: str
    native: Native | None = None


@dataclass(frozen=True)
class Image:
    jpeg: bytes


@dataclass(frozen=True)
class Audio:
    wav: bytes


@dataclass(frozen=True)
class Thought:
    """Display-only prose; ``native`` is the signed block or reasoning item the wire wants back, if any."""

    text: str
    native: Native | None = None


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: Json
    native: Native | None = None


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    text: str


Part = Text | Image | Audio | Thought | ToolCall | ToolResult


@dataclass(frozen=True)
class Message:
    role: Role
    parts: tuple[Part, ...]
    pin: bool = False  # "cache up to here": a hint adapters translate or ignore

    def texts(self) -> list[str]:
        return [part.text for part in self.parts if isinstance(part, Text)]

    def text(self) -> str:
        return "".join(self.texts())

    def thoughts(self) -> str:
        return "".join(part.text for part in self.parts if isinstance(part, Thought))

    def calls(self) -> list[ToolCall]:
        return [part for part in self.parts if isinstance(part, ToolCall)]

    def image_indexes(self) -> list[int]:
        return [i for i, part in enumerate(self.parts) if isinstance(part, Image)]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: Json  # JSON Schema


@dataclass(frozen=True)
class Request:
    system: str
    messages: tuple[Message, ...]
    tools: tuple[Tool, ...] = ()
    thinking: Thinking = Thinking.DEFAULT
    thought_summaries: bool = False  # stream the vendor's thought summaries back (where it has them)
    json_schema: Json | None = None
    max_tokens: int | None = None
    temperature: float | None = None
    pinned: str | None = None  # a handle from Pinned.pin: the pinned system and turns are not re-sent

    def parts(self) -> Iterable[Part]:
        for message in self.messages:
            yield from message.parts


@dataclass(frozen=True)
class Usage:
    prompt: int = 0
    cached: int = 0
    output: int = 0
    thinking: int = 0


@dataclass(frozen=True)
class Reply:
    message: Message
    usage: Usage
    finish: Finish
    detail: str = ""  # the vendor's own finish/block reason, for logs (MALFORMED_FUNCTION_CALL, SAFETY, …)


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ThoughtDelta:
    text: str


Event = TextDelta | ThoughtDelta | Reply


@dataclass(frozen=True)
class Capabilities:
    """What a wire can carry, declared up front so a request fails before it is sent."""

    wire: Wire
    audio_input: bool
    thought_summaries: bool
    json_schema: bool
    pinned: bool
    thinking_rungs: frozenset[Thinking]

    def check(self, request: Request) -> None:
        if not self.audio_input and any(isinstance(part, Audio) for part in request.parts()):
            raise LlmError.unsupported("audio input", self.wire)
        if not self.json_schema and request.json_schema is not None:
            raise LlmError.unsupported("JSON schema output", self.wire)
        if not self.pinned and request.pinned is not None:
            raise LlmError.unsupported("a pinned context", self.wire)

    def clamp(self, thinking: Thinking, model_rungs: frozenset[Thinking] = frozenset(LADDER)) -> Thinking:
        """The nearest rung both the wire and the model have, at or above the asked one (the top rung past its end)."""
        rungs = self.thinking_rungs & model_rungs or self.thinking_rungs
        if thinking == Thinking.DEFAULT or thinking in rungs:
            return thinking
        above = [rung for rung in LADDER[LADDER.index(thinking) :] if rung in rungs]
        if above:
            return above[0]
        return max(rungs, key=LADDER.index)


@dataclass(frozen=True)
class Model:
    """What one model accepts, declared up front — a catalog row, or its vendor's defaults for a name without one.

    ``vendor`` picks the wire and the route; the flags are the facts that would
    otherwise be name-matching inside an adapter.
    """

    name: str
    vendor: Vendor
    vision: bool = True
    thinking: frozenset[Thinking] = frozenset(LADDER)  # the rungs it accepts; the wire clamps within them
    budget_thinking: bool = False  # pre-adaptive Claude: thinking is a token budget, output_config.effort is a 400
    effort_with_tools: bool = True  # False: the chat wire 400s on tools + reasoning_effort (OpenAI's gpt-* there)

    def check(self, request: Request) -> None:
        if not self.vision and any(isinstance(part, Image) for part in request.parts()):
            raise LlmError(Kind.UNSUPPORTED, f"image input is not supported by {self.name}")
