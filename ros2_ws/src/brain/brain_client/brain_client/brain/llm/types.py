# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The vocabulary between the agent loop and whichever model backend it runs on.

The agent never builds or reads a request body: it hands a :class:`Conversation`
an observation and gets a :class:`Decision` back. Which provider is underneath,
and what its wire format looks like, stays behind this seam.

Two values still cross it as opaque ``dict`` payloads — a pending turn and a raw
response. Only the conversation that produced them may read them: a Gemini
thought signature and an OpenAI reasoning item both have to be echoed back
verbatim on the next request and cannot be rebuilt from a Decision.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from brain_client.common.enums import StrEnum

if TYPE_CHECKING:
    from collections.abc import Callable

    from brain_client.brain.utils import Frame

Usage = dict[str, int]
"""Token counts of the newest response, keyed ``prompt`` / ``cached`` /
``output``. Wire-visible: the trace snapshot publishes it as ``tokens``."""


class Provider(StrEnum):
    """Which vendor's API the brain thinks with (the ``brain_backend`` setting)."""

    GEMINI = "gemini"
    OPENAI = "openai"
    OPENAI_COMPAT = "openai_compat"  # any /v1/chat/completions server: NIM, vLLM, Ollama


class Backend(StrEnum):
    """How the brain reaches its provider (surfaced in health and telemetry)."""

    PROXY = "innate-proxy"
    GEMINI_DIRECT = "gemini-direct"
    OPENAI_DIRECT = "openai-direct"
    OPENAI_COMPAT = "openai-compat"
    UNCONFIGURED = "unconfigured"


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict
    id: str = ""


@dataclass(frozen=True)
class ToolSpec:
    """One function the model may call this turn.

    ``parameters`` is a JSON Schema object using the standard lowercase type
    names, or None for a no-argument tool. Each provider's wire module renders
    it — Gemini wants those type names uppercased, OpenAI takes them as they are.
    """

    name: str
    description: str
    parameters: dict | None = None


@dataclass
class Decision:
    """What the model wants the robot to do this turn."""

    speech: str | None = None
    thoughts: str | None = None
    calls: list[ToolCall] = field(default_factory=list)


class Conversation(Protocol):
    """A bounded model conversation: one :meth:`generate` per agent turn.

    Threading contract, which every implementation must keep: ``generate`` is
    the only blocking call and only *reads* history, so the agent awaits it on a
    worker thread. Every mutation (:meth:`commit`, :meth:`add_tool_outcomes`)
    runs on the agent's loop thread strictly between generate calls, and
    :meth:`clear` only while that loop is stopped — no concurrent access by
    construction, not by lock.
    """

    on_request: Callable[[dict], None] | None
    """Observability tap: called with the exact request body just before it goes
    on the wire, from generate's thread. The body must be treated as read-only —
    it shares structure with the live history."""

    @property
    def history_len(self) -> int: ...

    @property
    def image_turn_count(self) -> int:
        """History turns still carrying camera frames (pruning keeps the newest few)."""
        ...

    @property
    def last_usage(self) -> Usage: ...

    def open_turn(self, text: str, frames: list[Frame]) -> dict:
        """Serialize one observation. The result is provider-shaped and opaque:
        hand it back to :meth:`generate` and :meth:`commit`, never read it."""
        ...

    def generate(
        self,
        turn: dict,
        tools: list[ToolSpec],
        system: str,
        on_speech: Callable[[str], None] | None = None,
    ) -> dict:
        """Blocking network call — safe on a worker thread. Every plain-text
        delta is handed to ``on_speech`` as it arrives, which is what lets the
        robot start talking at the first sentence boundary."""
        ...

    def commit(self, turn: dict, response: dict) -> Decision:
        """Store the exchange in history and distil what the agent should act on."""
        ...

    def add_tool_outcomes(self, outcomes: list[tuple[ToolCall, str]]) -> None:
        """Answer the model's calls (every provider requires one response per call)."""
        ...

    def clear(self) -> None: ...


def parse_arguments(raw: object) -> dict:
    """Tool-call arguments as the OpenAI wire formats carry them: a JSON-encoded
    string, not an object. Anything unreadable is no arguments at all."""
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


_TOOL_NARRATION = re.compile(r"Calling tool\b")


def split_tool_narration(text: str) -> tuple[str, bool]:
    """Cut leaked tool-call narration ("Calling tool ..." to end of text).

    gemini-3 preview sometimes appends it to its reply, without a sentence
    boundary. Returns ``(clean text, whether narration was found)`` — the one
    scrub both the chat transcript (:func:`clean_speech`) and the audio path
    (``SpeechStreamer._say``) apply, so the two can never diverge.
    """
    match = _TOOL_NARRATION.search(text)
    if match is None:
        return text, False
    return text[: match.start()].rstrip(), True


def clean_speech(speech: str | None) -> str | None:
    """Drop unspeakable output: placeholders and leaked tool-call narration."""
    if not speech:
        return None
    speech, _ = split_tool_narration(speech)
    return speech if re.search(r"[a-zA-Z0-9]", speech) else None
