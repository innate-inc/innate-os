# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The scribe's answer as data: the proposal the rules are written against, and
the parse that turns one Gemini response into it. Everything here is a claim,
never a write — an unreadable answer comes back as None rather than as a
half-filled proposal. PURE module: no transport, no I/O."""

from __future__ import annotations

import json
from dataclasses import dataclass

from brain_client.common.enums import StrEnum
from brain_client.people.memory import (
    Attribution,
    FactKind,
    as_dict,
    as_float,
    as_list,
    enum_member,
)


class Introduction(StrEnum):
    """How a name arrived — only these three can commit one (RFC 6.3)."""

    SELF = "self"
    OWNER = "owner"
    THIRD_PARTY = "third_party"
    OTHER = "other"


@dataclass(frozen=True)
class ScribeFact:
    who: str
    text: str
    kind: FactKind = FactKind.BIOGRAPHY
    confidence: float = 0.5
    attribution: Attribution = Attribution.UNCERTAIN
    quote: str = ""
    utterance: str = ""
    supersedes: str = ""


@dataclass(frozen=True)
class ScribeName:
    who: str
    name: str
    confidence: float = 0.0
    quote: str = ""
    utterance: str = ""
    introduction: Introduction = Introduction.OTHER
    referent: str = ""
    correction: bool = False


@dataclass(frozen=True)
class ScribeLoop:
    who: str
    text: str
    due: str | None = None
    quote: str = ""
    utterance: str = ""


@dataclass(frozen=True)
class ScribeNote:
    who: str
    text: str


@dataclass(frozen=True)
class ScribeOutput:
    facts: tuple[ScribeFact, ...] = ()
    name_candidates: tuple[ScribeName, ...] = ()
    open_loops: tuple[ScribeLoop, ...] = ()
    episode_note: str = ""
    appearance_notes: tuple[ScribeNote, ...] = ()
    sensitive: tuple[ScribeFact, ...] = ()


def parse_output(response: dict) -> ScribeOutput | None:
    """The model's JSON, or None when the answer is unreadable."""
    data = response_json(response)
    if data is None:
        return None
    return ScribeOutput(
        facts=tuple(_fact(entry) for entry in as_list(data.get("facts")) if as_dict(entry).get("text")),
        name_candidates=tuple(
            _name(entry) for entry in as_list(data.get("name_candidates")) if as_dict(entry).get("name")
        ),
        open_loops=tuple(_loop(entry) for entry in as_list(data.get("open_loops")) if as_dict(entry).get("text")),
        episode_note=str(data.get("episode_note") or ""),
        appearance_notes=tuple(
            ScribeNote(who=str(as_dict(entry).get("who", "")), text=str(as_dict(entry).get("text", "")))
            for entry in as_list(data.get("appearance_notes"))
            if as_dict(entry).get("text")
        ),
        sensitive=tuple(
            _fact(entry, kind=FactKind.SENSITIVE)
            for entry in as_list(data.get("sensitive"))
            if as_dict(entry).get("text")
        ),
    )


def response_json(response: dict) -> dict | None:
    """The JSON object a Gemini answer carries, or None when there is none to
    read — the scribe's window call and the recall call both land here."""
    try:
        parts = response["candidates"][0]["content"]["parts"]
        text = next(part["text"] for part in parts if part.get("text") and not part.get("thought"))
        data = json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError, StopIteration):
        return None
    return data if isinstance(data, dict) else None


def _fact(entry: object, kind: FactKind | None = None) -> ScribeFact:
    data = as_dict(entry)
    return ScribeFact(
        who=str(data.get("who", "")),
        text=str(data.get("text", "")),
        kind=kind or enum_member(FactKind, data.get("kind"), FactKind.BIOGRAPHY),
        confidence=as_float(data.get("confidence"), 0.5),
        attribution=enum_member(Attribution, data.get("attribution"), Attribution.UNCERTAIN),
        quote=str(data.get("quote", "")),
        utterance=str(data.get("utterance", "")),
        supersedes=str(data.get("supersedes") or ""),
    )


def _name(entry: object) -> ScribeName:
    data = as_dict(entry)
    return ScribeName(
        who=str(data.get("who", "")),
        name=str(data.get("name", "")).strip(),
        confidence=as_float(data.get("confidence"), 0.0),
        quote=str(data.get("quote", "")),
        utterance=str(data.get("utterance", "")),
        introduction=enum_member(Introduction, data.get("introduction"), Introduction.OTHER),
        referent=str(data.get("referent") or ""),
        correction=bool(data.get("correction", False)),
    )


def _loop(entry: object) -> ScribeLoop:
    data = as_dict(entry)
    return ScribeLoop(
        who=str(data.get("who", "")),
        text=str(data.get("text", "")),
        due=str(data.get("due")) if data.get("due") else None,
        quote=str(data.get("quote", "")),
        utterance=str(data.get("utterance", "")),
    )
