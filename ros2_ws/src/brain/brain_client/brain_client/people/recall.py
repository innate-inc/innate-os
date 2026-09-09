# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Deep recall (RFC 6.4): whether a line reaches for the past at all, who it is
asking about, the one Gemini call over that person's whole record, and the
answer travelling back to the thread that publishes it. PURE module: the
transport is the caller's, and so is the record it is handed."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from brain_client.people.scribe_output import response_json
from brain_client.people.types import TAG_RE

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from brain_client.people.types import RosterEntryDict, TrackState

_MEMORY_VERB = re.compile(
    r"\b(remember|remembered|remembers|recall|recalled|forgot|forgotten|last time|the other day|"
    r"when did|what did|who did|did (i|we|he|she|they|you) (say|tell|ask|mention)|used to|"
    r"told (me|us|you)|mentioned|talked about|talk about|know about)\b"
)
_PRONOUN = re.compile(r"\b(you|they|them|their|he|him|his|she|her|hers)\b")
_TAG = re.compile(r"\bP\d+\b")

RECALL_SYSTEM = (
    "You are the long-term memory of a small home robot. You are given everything the robot has "
    "recorded about one person — profile, dated facts, episodes and open loops — and a question "
    "that was just asked out loud. Answer only from that record, in one or two short sentences, "
    "as a note to the robot ('Ana asked for the blue socks on Tuesday'). Never guess, never infer "
    "traits, and set found to false when the record does not answer the question."
)

_RECALL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "found": {"type": "BOOLEAN"},
        "answer": {"type": "STRING", "description": "one or two sentences, empty when found is false"},
    },
    "required": ["found", "answer"],
}


@dataclass(frozen=True)
class Recall:
    """What a deep recall came back with, on its way from the scribe thread to
    the engine thread. It crosses as data because :class:`PeopleEvents` reads
    then writes its cooldown dicts, and only the engine thread may touch it."""

    person_id: str
    name: str | None
    text: str
    stamp: float


def mentions_memory(text: str) -> bool:
    """Whether the line even reaches for the past — the cheap half of
    :func:`is_memory_question`, so a caller can skip building a roster for the
    chat lines (almost all of them) that are not memory questions."""
    return _MEMORY_VERB.search(text.lower()) is not None


def is_memory_question(text: str, known_names: Iterable[str]) -> str | None:
    """Who a question is asking the robot to remember about, or None when it is
    not a memory question. A known name or a ``P<n>`` tag names the person; a
    bare pronoun ("did they say …") comes back as that pronoun, meaning
    whoever is in view — the caller resolves it against the snapshot."""
    lowered = text.lower()
    if not _MEMORY_VERB.search(lowered):
        return None
    for name in sorted({name for name in known_names if name}, key=len, reverse=True):
        if re.search(rf"\b{re.escape(name.lower())}\b", lowered):
            return name
    tag = _TAG.search(text)
    if tag is not None:
        return tag.group(0)
    pronoun = _PRONOUN.search(lowered)
    return pronoun.group(0) if pronoun is not None else None


def recall_person(subject: str, tracks: Sequence[TrackState], roster: Sequence[RosterEntryDict]) -> str | None:
    """Who a memory question is about (RFC 6.4). ``is_memory_question`` hands
    back a name, a ``P<n>`` tag or a bare pronoun; a pronoun resolves against
    who is in view, and only when exactly one person is."""
    if TAG_RE.match(subject):
        track = next((t for t in tracks if t.tag.upper() == subject.upper() and not t.lost), None)
        return track.identity.person_id if track is not None else None
    wanted = subject.strip().casefold()
    for entry in roster:
        if (entry.get("name") or "").strip().casefold() == wanted and wanted:
            return entry.get("person_id")
    in_view = {track.identity.person_id for track in tracks if not track.lost and track.identity.person_id}
    return in_view.pop() if len(in_view) == 1 else None


def alone_in_view(person_id: str, tracks: Sequence[TrackState]) -> bool:
    """Whether the subject of a recall is the only person the robot can see.
    The scribe keeps sensitive facts out of an answer that anyone else is
    standing there to hear (RFC 6.3), the same one-plausible-referent rule that
    gates writing one."""
    live = [track for track in tracks if not track.lost]
    return len(live) == 1 and live[0].identity.person_id == person_id


def recall_request(person_memory: dict, question: str) -> dict:
    """One Gemini call over a person's whole memory (RFC 6.4, deep recall)."""
    return {
        "systemInstruction": {"parts": [{"text": RECALL_SYSTEM}]},
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": f"What the robot has recorded:\n{json.dumps(person_memory, sort_keys=True)}"},
                    {"text": f'The question just asked: "{question}"'},
                ],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _RECALL_SCHEMA,
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }


def parse_recall(response: dict) -> str:
    """The recalled sentence, or empty when nothing was found or the answer was
    unreadable — an empty recall raises no event."""
    data = response_json(response)
    if data is None or not data.get("found"):
        return ""
    return str(data.get("answer") or "").strip()
