# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""What the brain's conversation carries, decided above the wire: pure functions on message tuples.

The robot's own rules — which camera frames ride, the wrist camera's
newest-only rule, when to evict — live here, not in the provider library.
Every function returns new values; the caller replaces its history with the
result. Adapters translate the hints these leave (``pin``) or ignore them —
they never decide what to send.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from innate_llm.types import Image, Message, Request, Role, Text, Thought

FRAME_REMOVED = "[older camera frame removed]"
WRIST_FRAME_REMOVED = "[older wrist camera frame removed]"

History = tuple[Message, ...]


def masked(message: Message, indexes: Sequence[int], placeholder: str) -> Message:
    """A copy of ``message`` with the frames at ``indexes`` (positions in its parts) replaced by ``placeholder``."""
    parts = tuple(
        Text(placeholder) if i in indexes and isinstance(part, Image) else part for i, part in enumerate(message.parts)
    )
    return replace(message, parts=parts)


def image_turns(messages: Sequence[Message]) -> list[Message]:
    return [m for m in messages if m.role == Role.USER and m.image_indexes()]


def window_images(messages: History, keep: int) -> History:
    """Mask every frame outside the newest ``keep`` frame-bearing turns (all of them when ``keep`` is 0)."""
    turns = image_turns(messages)
    stale = turns[:-keep] if keep > 0 else turns
    return replace_each(messages, {id(m): masked(m, m.image_indexes(), FRAME_REMOVED) for m in stale})


def mask_latest_only(messages: History, turn: Message, indexes: Sequence[int]) -> History:
    """``turn`` (matched by identity) with the frames at ``indexes`` masked — the wrist camera's newest-only rule."""
    return replace_each(messages, {id(turn): masked(turn, indexes, WRIST_FRAME_REMOVED)})


def strip_thoughts(messages: History) -> History:
    """Drop every Thought — and with it the signed block or reasoning item it carried as ``native``.

    Always accepted by every wire (Gemini keeps its signatures on the parts that
    follow), which is what makes it the edit that keeps a rebuilt prefix valid.
    """
    return tuple(
        replace(m, parts=tuple(p for p in m.parts if not isinstance(p, Thought)))
        if any(isinstance(p, Thought) for p in m.parts)
        else m
        for m in messages
    )


def evict_to(messages: History, keep: int) -> History:
    """The newest ``keep`` messages, then the oldest dropped until the history starts with a user turn.

    A leading assistant turn or an orphaned tool result (its call just evicted) is rejected by every vendor.
    """
    kept = messages[max(len(messages) - keep, 0) :]
    while kept and kept[0].role != Role.USER:
        kept = kept[1:]
    return kept


def pin_prefix(request: Request) -> Request:
    """``pin=True`` on the first turn (covers the system prompt and tools), the last tool-result turn, and the newest turn.

    Three breakpoints that move only when the prefix does: what Claude's cache_control wants, and what
    the implicit caches (Gemini, OpenAI) get for free from the same discipline.
    """
    messages = request.messages
    if not messages:
        return request
    pins = {0, len(messages) - 1}
    tool_turns = [i for i, m in enumerate(messages) if m.role == Role.TOOL]
    if tool_turns:
        pins.add(tool_turns[-1])
    pinned = tuple(replace(m, pin=True) if i in pins and not m.pin else m for i, m in enumerate(messages))
    return replace(request, messages=pinned)


def replace_each(messages: History, replacements: dict[int, Message]) -> History:
    """``messages`` with the objects keyed by ``id()`` swapped for their replacements."""
    if not replacements:
        return tuple(messages)
    return tuple(replacements.get(id(m), m) for m in messages)
