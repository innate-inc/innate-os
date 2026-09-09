# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The scribe: the agent watching the conversation and writing down what is
worth keeping (RFC 6.3, docs/rfc/people-memory.md in innate-jetson). It buffers
``/brain/chat_in`` and ``/brain/chat_out`` into windows with the tags in view
per message, spends one Gemini call per window, and treats the answer as a
proposal: :func:`apply` is the rule set that decides what is written, which is
why there is no ``remember_person`` tool. PURE: no rclpy, no network of its
own — the transport is injected as ``(path, body, timeout) -> dict`` (what
:class:`brain_client.brain.transport.GeminiRest`'s ``post`` is); stamps are
epoch seconds."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, TypeVar

from brain_client.brain.transport import GENERATE_PATH
from brain_client.common.enums import StrEnum
from brain_client.people.memory import (
    FACT_TEXT_LIMIT,
    Attribution,
    ConsentPath,
    FactKind,
    FactSource,
    profile_to_dict,
)
from brain_client.people.store import DIR_MODE
from brain_client.people.types import IdentityState

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

    from brain_client.people.memory import Fact
    from brain_client.people.store import PeopleStore

_E = TypeVar("_E", bound=StrEnum)

Transport = Callable[[str, dict, float | None], dict]
"""(api path, request body, timeout) -> parsed response; GeminiRest.post's shape."""

Revoked = Callable[[str | None], bool]
"""Whether a person id has been forgotten; ``None`` (an untracked view) never is."""

WINDOW_IDLE_SEC = 8.0
WINDOW_MAX_MESSAGES = 12
QUEUE_HORIZON_SEC = 3600.0  # one hour of windows survives an outage; older ones are dropped
QUEUE_MAX_WINDOWS = 240
SCRIBE_TIMEOUT_SEC = 20.0
RECALL_TIMEOUT_SEC = 20.0
_MAX_CONTEXT_FACTS = 20  # the existing facts the scribe is shown per person, so it can supersede


class Speaker(StrEnum):
    """Who said a line. The node maps chat entries onto these: ``user`` from
    ``/brain/chat_in``, ``robot`` from the robot's own speech and skill output."""

    USER = "user"
    ROBOT = "robot"


class ChangeKind(StrEnum):
    """What one applied window did; the node turns some of these into events."""

    FACT = "fact"
    FACT_SUPERSEDED = "fact_superseded"
    NAME = "name"
    NAME_CANDIDATE = "name_candidate"
    OPEN_LOOP = "open_loop"
    APPEARANCE = "appearance"
    EPISODE_NOTE = "episode_note"
    REJECTED = "rejected"


class Introduction(StrEnum):
    """How a name arrived — only these three can commit one (RFC 6.3)."""

    SELF = "self"
    OWNER = "owner"
    THIRD_PARTY = "third_party"
    OTHER = "other"


@dataclass(frozen=True)
class TagView:
    """One track as it stood when a message arrived: what the scribe is allowed
    to attribute to, and the state the name rules test."""

    tag: str
    state: IdentityState = IdentityState.UNKNOWN
    person_id: str | None = None
    name: str | None = None
    enrolling: bool = False

    @property
    def nameable(self) -> bool:
        """Face-confirmed or currently enrolling — the only tracks a passive
        name may land on."""
        return self.enrolling or self.state in (IdentityState.KNOWN, IdentityState.FAMILIAR)


@dataclass(frozen=True)
class Utterance:
    id: str
    stamp: float
    speaker: Speaker
    text: str
    in_view: tuple[TagView, ...] = ()

    def view(self, tag: str) -> TagView | None:
        return next((seen for seen in self.in_view if seen.tag == tag), None)


@dataclass(frozen=True)
class Window:
    """One closed conversation window: the messages and who was in view for
    each of them."""

    messages: tuple[Utterance, ...]

    @property
    def opened(self) -> float:
        return self.messages[0].stamp if self.messages else 0.0

    @property
    def closed(self) -> float:
        return self.messages[-1].stamp if self.messages else 0.0

    def views(self) -> dict[str, TagView]:
        """The latest view of every tag seen anywhere in the window."""
        views: dict[str, TagView] = {}
        for message in self.messages:
            for seen in message.in_view:
                views[seen.tag] = seen
        return views

    def person_ids(self) -> list[str]:
        return list(dict.fromkeys(view.person_id for view in self.views().values() if view.person_id))


@dataclass(frozen=True)
class Change:
    """One thing the window changed (or refused to change)."""

    kind: ChangeKind
    tag: str
    person_id: str | None = None
    text: str = ""
    reason: str = ""
    record_id: str = ""


# ------------------------------------------------------------ window buffer


def line_without_revoked(message: Utterance, revoked: Revoked) -> Utterance | None:
    """One transcript line with a forgotten person's view taken out, or None
    when the line was only about them (RFC section 10)."""
    kept = tuple(view for view in message.in_view if not revoked(view.person_id))
    if len(kept) == len(message.in_view):
        return message
    return replace(message, in_view=kept) if kept else None


def without_revoked(window: Window, revoked: Revoked) -> Window:
    """The window as it stands after a forget: deletion has to reach the work
    already in flight, or it lands a moment later anyway."""
    lines = (line_without_revoked(message, revoked) for message in window.messages)
    return Window(tuple(line for line in lines if line is not None))


class WindowBuffer:
    """Accumulates messages into a window; closes it 8 s after the last message
    or at 12 messages, whichever comes first."""

    def __init__(self, *, idle_sec: float = WINDOW_IDLE_SEC, max_messages: int = WINDOW_MAX_MESSAGES):
        self._idle_sec = idle_sec
        self._max_messages = max_messages
        self._messages: list[Utterance] = []

    def add(self, message: Utterance) -> Window | None:
        """Buffer a message, returning the window when this one fills it."""
        self._messages.append(message)
        if len(self._messages) < self._max_messages:
            return None
        return self._close()

    def due(self, now: float) -> Window | None:
        """The window if the conversation has been quiet long enough."""
        if not self._messages or now - self._messages[-1].stamp < self._idle_sec:
            return None
        return self._close()

    def pending(self) -> int:
        return len(self._messages)

    def clear(self) -> None:
        """Throw the buffer away: collection went off, and these lines were
        never meant to be kept."""
        self._messages = []

    def forget(self, person_id: str) -> None:
        """Take a forgotten person out of the buffered lines before they can be
        sent anywhere (RFC section 10)."""
        self._messages = list(without_revoked(Window(tuple(self._messages)), lambda who: who == person_id).messages)

    def _close(self) -> Window:
        window = Window(tuple(self._messages))
        self._messages = []
        return window


# ------------------------------------------------------- the Gemini request

SYSTEM_TEXT = (
    "You are the scribe of a small home robot. You read a short window of the conversation the "
    "robot just had, together with who was visible (tagged P1, P2 …) for each line and what the "
    "robot already knows about them, and you write down only what is worth remembering about a "
    "person.\n"
    "Rules:\n"
    "- Report only what was actually said. Never infer emotion, mood, ethnicity, gender, age, "
    "health or any other attribute of a person from how they look or sound; if it was not said, "
    "it does not exist.\n"
    "- Attribute every item to the tag of the person it is about, and quote the exact words it "
    "came from together with that line's utterance id. If you cannot tell which visible person a "
    'statement is about, still give your best tag but set attribution to "uncertain".\n'
    '- attribution: "self" when the person said it about themselves, "third_party" when '
    'somebody else said it about them, "owner" when the robot\'s owner did, "robot" when the '
    "robot itself concluded it out loud.\n"
    '- When a statement updates something already on file, set "supersedes" to that fact\'s id '
    "instead of writing a near-duplicate.\n"
    '- name_candidates: report any name heard for a visible person. Set "introduction" to '
    '"self" for a first-person introduction ("I\'m Ana"), "owner" when the owner names them, '
    '"third_party" when somebody introduces them ("this is my daughter Zoe"), "other" otherwise. '
    'Set "referent" to the tag the transcript itself identifies, or leave it empty when the words '
    'do not say which visible person is meant. Set "correction" when the words correct a name '
    "already on file. Never guess which person a name belongs to; the robot resolves that itself.\n"
    "- sensitive: health, finances, relationships in trouble, anything a person would not want "
    "repeated. Put those there rather than in facts, whether or not they asked the robot to "
    "remember them.\n"
    '- open_loops: things promised, requested or due ("find the blue socks by tomorrow").\n'
    "- episode_note: at most two sentences on what happened in this window, or an empty string.\n"
    "- Return empty lists when the window holds nothing worth keeping. That is the normal case."
)

_FACT_ITEM = {
    "type": "OBJECT",
    "properties": {
        "who": {"type": "STRING", "description": "the tag of the person the item is about, e.g. P2"},
        "kind": {
            "type": "STRING",
            "enum": ["identity", "preference", "biography", "relationship", "routine", "request"],
        },
        "text": {"type": "STRING", "description": f"at most {FACT_TEXT_LIMIT} characters"},
        "confidence": {"type": "NUMBER"},
        "attribution": {"type": "STRING", "enum": ["self", "third_party", "robot", "owner", "uncertain"]},
        "quote": {"type": "STRING", "description": "the exact words this came from"},
        "utterance": {"type": "STRING", "description": "the id of the line quoted"},
        "supersedes": {"type": "STRING", "description": "id of the fact this replaces, else empty"},
    },
    "required": ["who", "kind", "text", "confidence", "attribution", "quote", "utterance", "supersedes"],
}

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "facts": {"type": "ARRAY", "items": _FACT_ITEM},
        "name_candidates": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "who": {"type": "STRING"},
                    "name": {"type": "STRING"},
                    "confidence": {"type": "NUMBER"},
                    "quote": {"type": "STRING"},
                    "utterance": {"type": "STRING"},
                    "introduction": {"type": "STRING", "enum": ["self", "owner", "third_party", "other"]},
                    "referent": {"type": "STRING", "description": "tag the transcript names, else empty"},
                    "correction": {"type": "BOOLEAN"},
                },
                "required": ["who", "name", "confidence", "quote", "utterance", "introduction", "referent"],
            },
        },
        "open_loops": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "who": {"type": "STRING"},
                    "text": {"type": "STRING"},
                    "due": {"type": "STRING", "description": "YYYY-MM-DD or empty"},
                    "quote": {"type": "STRING"},
                    "utterance": {"type": "STRING"},
                },
                "required": ["who", "text", "due"],
            },
        },
        "episode_note": {"type": "STRING"},
        "appearance_notes": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"who": {"type": "STRING"}, "text": {"type": "STRING"}},
                "required": ["who", "text"],
            },
        },
        "sensitive": {"type": "ARRAY", "items": _FACT_ITEM},
    },
    "required": ["facts", "name_candidates", "open_loops", "episode_note", "appearance_notes", "sensitive"],
}

_GENERATION_CONFIG = {
    "responseMimeType": "application/json",
    "responseSchema": RESPONSE_SCHEMA,
    "thinkingConfig": {"thinkingLevel": "low"},
}


def build_request(window: Window, known_facts: Mapping[str, Sequence[Fact]] | None = None) -> dict:
    """The one Gemini call per window: the transcript, who was in view for each
    line, and the facts already on file for them (so the model can supersede
    rather than duplicate). ``known_facts`` is keyed by tag."""
    return {
        "systemInstruction": {"parts": [{"text": SYSTEM_TEXT}]},
        "contents": [{"role": "user", "parts": [{"text": window_text(window, known_facts or {})}]}],
        "generationConfig": _GENERATION_CONFIG,
    }


def window_text(window: Window, known_facts: Mapping[str, Sequence[Fact]]) -> str:
    """The window as the model reads it."""
    lines = ["People visible during this window:"]
    views = window.views()
    if not views:
        lines.append("- nobody was in view")
    for tag, view in sorted(views.items()):
        name = view.name or "unknown"
        lines.append(f"- {tag} = {name} ({view.state}{', enrolling' if view.enrolling else ''})")
        for fact in list(known_facts.get(tag, ()))[:_MAX_CONTEXT_FACTS]:
            lines.append(f"    on file [{fact.id}] {fact.kind}: {fact.text}")
    lines.append("")
    lines.append("Transcript:")
    for message in window.messages:
        in_view = ", ".join(seen.tag for seen in message.in_view) or "nobody"
        lines.append(f'[{message.id}] {message.speaker} (in view: {in_view}): "{message.text}"')
    lines.append("")
    lines.append("What is worth remembering about the people in view?")
    return "\n".join(lines)


# ------------------------------------------------------------- the response


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
    data = _response_json(response)
    if data is None:
        return None
    return ScribeOutput(
        facts=tuple(_fact(entry) for entry in _list(data.get("facts")) if _dict(entry).get("text")),
        name_candidates=tuple(_name(entry) for entry in _list(data.get("name_candidates")) if _dict(entry).get("name")),
        open_loops=tuple(_loop(entry) for entry in _list(data.get("open_loops")) if _dict(entry).get("text")),
        episode_note=str(data.get("episode_note") or ""),
        appearance_notes=tuple(
            ScribeNote(who=str(_dict(entry).get("who", "")), text=str(_dict(entry).get("text", "")))
            for entry in _list(data.get("appearance_notes"))
            if _dict(entry).get("text")
        ),
        sensitive=tuple(
            _fact(entry, kind=FactKind.SENSITIVE) for entry in _list(data.get("sensitive")) if _dict(entry).get("text")
        ),
    )


# --------------------------------------------------------------- the rules

_REMEMBER = re.compile(
    r"\b(remember|memorise|memorize|don'?t forget|do not forget|make a note|note that|keep in mind|"
    r"write (this|that) down)\b"
)
_NOT_REMEMBER = re.compile(
    r"\b((do|did|does|would|will|can|could)\s?(not|n'?t)|never|stop|no need to|rather not)"
    r"\s+(?:\w+\s+){0,3}?(remember|note|keep|memoris|memoriz)"
)
_RECALLING = re.compile(
    r"\bremember when\b|\b(do|did|does|can|could|would|will|have)\s+(you|we|i|they|he|she)\s+"
    r"(?:\w+\s+){0,2}?(remember|recall)\b"
)
_COUNTS = {2: "two", 3: "three", 4: "four", 5: "five"}


def apply(output: ScribeOutput, window: Window, store: PeopleStore, now: float) -> list[Change]:
    """Turn one window's proposal into writes, under every RFC 6.3 rule. The
    returned changes are what the block and the events show; a refusal is a
    :attr:`ChangeKind.REJECTED` change carrying its reason, never a silent
    drop."""
    views = window.views()
    changes: list[Change] = []
    for name in output.name_candidates:
        changes.append(_apply_name(name, window, views, store, now))
    for fact in (*output.facts, *output.sensitive):
        changes.append(_apply_fact(fact, window, views, store, now))
    for loop in output.open_loops:
        changes.append(_apply_loop(loop, window, views, store, now))
    for note in output.appearance_notes:
        changes.append(_apply_appearance(note, views, store, now))
    changes.extend(_apply_episode_note(output.episode_note, views, store, now))
    return changes


def _apply_fact(
    fact: ScribeFact, window: Window, views: Mapping[str, TagView], store: PeopleStore, now: float
) -> Change:
    view = views.get(fact.who)
    if view is None or view.person_id is None:
        return Change(ChangeKind.REJECTED, fact.who, None, fact.text, reason="no tracked person for that tag")
    person_id = view.person_id
    quoted = _quoted_message(window, fact.utterance, fact.quote)
    if quoted is None:
        return Change(ChangeKind.REJECTED, fact.who, person_id, fact.text, reason="the quote is not in this window")
    # RFC 6.3: a fact is written only if its subject was in view during the line
    # it was quoted from. Writing it anyway puts the room's words on one
    # person's record, and lets them supersede a fact that was really theirs.
    if quoted.view(fact.who) is None:
        return Change(ChangeKind.REJECTED, fact.who, person_id, fact.text, reason="not in view during the quoted line")
    if fact.kind is FactKind.SENSITIVE and not _consented(window, fact.who, quoted):
        return Change(
            ChangeKind.REJECTED,
            fact.who,
            person_id,
            fact.text,
            reason="sensitive and nobody asked the robot to remember it",
        )
    source = FactSource(utterance_id=fact.utterance, stamp=quoted.stamp, quote=fact.quote, speaker_tag=fact.who)
    importance = _importance(fact.kind, fact.confidence)
    existing = _fact_ids(store, person_id)
    if fact.supersedes and fact.supersedes in existing:
        record = store.supersede_fact(
            person_id,
            fact.supersedes,
            fact.text,
            fact.kind,
            now=now,
            attribution=fact.attribution,
            source=source,
            confidence=fact.confidence,
            importance=importance,
        )
        kind = ChangeKind.FACT_SUPERSEDED
    else:
        record = store.add_fact(
            person_id,
            fact.text,
            fact.kind,
            now=now,
            attribution=fact.attribution,
            source=source,
            confidence=fact.confidence,
            importance=importance,
        )
        kind = ChangeKind.FACT
    if record is None:
        return Change(ChangeKind.REJECTED, fact.who, person_id, fact.text, reason="the store refused the fact")
    return Change(kind, fact.who, person_id, fact.text, record_id=record)


def _apply_name(
    name: ScribeName, window: Window, views: Mapping[str, TagView], store: PeopleStore, now: float
) -> Change:
    quoted = _quoted_message(window, name.utterance, name.quote)
    referents = _referents(name, views, quoted)
    if not referents:
        return Change(ChangeKind.REJECTED, name.who, None, name.name, reason="no tracked person for that tag")
    if name.introduction is Introduction.OTHER:
        return _candidate(name, referents, store, now, reason="the words are not an introduction")
    if quoted is None:
        # Without the line it came from there is no way to check who was in
        # view when it was said, and a name is never committed on trust.
        return _candidate(name, referents, store, now, reason="the quoted words are not in this window")
    if len(referents) > 1:
        return _candidate(name, referents, store, now, reason="more than one person could be the referent")
    view = referents[0]
    if view.person_id is None:
        return Change(ChangeKind.REJECTED, view.tag, None, name.name, reason="no tracked person for that tag")
    person_id = view.person_id
    if not view.nameable:
        return _candidate(name, referents, store, now, reason="the track is neither confirmed nor enrolling")
    # RFC 6.3: the robot's own use of a name is reinforcing evidence, never the
    # trigger — it may only settle a name a person was already heard to give.
    if quoted.speaker is Speaker.ROBOT and not _heard_before(store, person_id, name.name):
        return _candidate(name, referents, store, now, reason="only the robot said the name, and nobody else has")
    on_file = store.name_of(person_id)
    correcting = bool(on_file) and on_file != name.name
    if correcting and not (name.correction and name.introduction in (Introduction.SELF, Introduction.OWNER)):
        return _candidate(
            name,
            referents,
            store,
            now,
            reason=f"another name is on file ({on_file})",
            hint=f"heard '{name.quote}' but {view.tag} is already {on_file}; ask if that is a correction",
        )
    source = ConsentPath.APP if name.introduction is Introduction.OWNER else ConsentPath.CONVERSATION
    store.rename(person_id, name.name, str(source), now=now)
    store.clear_name_candidates(person_id, now)
    if correcting:
        return Change(
            ChangeKind.NAME,
            view.tag,
            person_id,
            f'{view.tag} corrected "{on_file}" to "{name.name}" (from "{name.quote}")',
            reason="correction",
        )
    return Change(
        ChangeKind.NAME,
        view.tag,
        person_id,
        f'{view.tag} said "{name.quote}" — {view.tag} = {name.name} from here on',
    )


def _candidate(
    name: ScribeName,
    referents: Sequence[TagView],
    store: PeopleStore,
    now: float,
    *,
    reason: str,
    hint: str | None = None,
) -> Change:
    """A name that did not meet the commit rules: stored against every person
    it could belong to, and surfaced as a question the agent may ask."""
    for view in referents:
        if view.person_id is not None:
            store.add_name_candidate(
                view.person_id, name.name, now=now, quote=name.quote, tag=view.tag, confidence=name.confidence
            )
    return Change(
        ChangeKind.NAME_CANDIDATE,
        referents[0].tag,
        referents[0].person_id,
        hint or disambiguation_hint(name.quote, len(referents)),
        reason=reason,
    )


def disambiguation_hint(quote: str, referents: int) -> str:
    """The line the block carries when a name could not be committed."""
    if referents > 1:
        people = _COUNTS.get(referents, str(referents))
        return f"heard '{quote}' but {people} people are in view; if it matters, ask which one"
    return f"heard '{quote}' but could not tell who it belongs to; if it matters, ask"


def _apply_loop(
    loop: ScribeLoop, window: Window, views: Mapping[str, TagView], store: PeopleStore, now: float
) -> Change:
    view = views.get(loop.who)
    if view is None or view.person_id is None:
        return Change(ChangeKind.REJECTED, loop.who, None, loop.text, reason="no tracked person for that tag")
    person_id = view.person_id
    cited = bool(loop.utterance or loop.quote)
    quoted = _quoted_message(window, loop.utterance, loop.quote)
    if cited and (quoted is None or quoted.view(loop.who) is None):
        # Same rule as a fact: a promise the model cites is only this person's
        # if they were in view while it was made (RFC 6.3).
        return Change(ChangeKind.REJECTED, loop.who, person_id, loop.text, reason="not in view during the quoted line")
    record = store.add_open_loop(person_id, loop.text, now=now, due=loop.due or None, source="scribe")
    if record is None:
        return Change(ChangeKind.REJECTED, loop.who, person_id, loop.text, reason="the store refused the loop")
    return Change(ChangeKind.OPEN_LOOP, loop.who, person_id, loop.text, record_id=record)


def _apply_appearance(note: ScribeNote, views: Mapping[str, TagView], store: PeopleStore, now: float) -> Change:
    view = views.get(note.who)
    if view is None or view.person_id is None:
        return Change(ChangeKind.REJECTED, note.who, None, note.text, reason="no tracked person for that tag")
    person_id = view.person_id
    # An appearance note is about today's encounter, not about the person: it
    # rides the profile as a low-importance note and never the digest.
    record = store.add_fact(
        person_id,
        note.text,
        FactKind.APPEARANCE,
        now=now,
        attribution=Attribution.ROBOT,
        source=FactSource(stamp=now),
        confidence=0.5,
        importance=0.2,
    )
    if record is None:
        return Change(ChangeKind.REJECTED, note.who, person_id, note.text, reason="the store refused the note")
    return Change(ChangeKind.APPEARANCE, note.who, person_id, note.text, record_id=record)


def _apply_episode_note(note: str, views: Mapping[str, TagView], store: PeopleStore, now: float) -> list[Change]:
    if not note.strip():
        return []
    changes: list[Change] = []
    for tag, view in sorted(views.items()):
        if view.person_id is not None and store.note_episode(view.person_id, note, now):
            changes.append(Change(ChangeKind.EPISODE_NOTE, tag, view.person_id, note.strip()))
    return changes


def _referents(name: ScribeName, views: Mapping[str, TagView], quoted: Utterance | None) -> list[TagView]:
    """Everyone the name could belong to: whoever was in view while it was
    said, narrowed to the tag the transcript itself names when that tag was one
    of them and can hold a name. Without the line it came from there is no view
    to check against, so the candidates are the window's — and ``_apply_name``
    commits none of them."""
    if quoted is None:
        view = views.get(name.referent) or views.get(name.who)
        return [view] if view is not None else list(views.values())
    in_view = list(quoted.in_view)
    named = next((view for view in in_view if view.tag == name.referent), None) if name.referent else None
    return [named] if named is not None and named.nameable else in_view


def _quoted_message(window: Window, utterance_id: str, quote: str) -> Utterance | None:
    """The line a quote came from: the line the id names when it really carries
    those words, else the line that does. A quote the transcript does not
    contain came from nowhere, and every gate below rests on it."""
    needle = _normalize(quote)
    if not needle:
        return None
    if utterance_id:
        cited = next((message for message in window.messages if message.id == utterance_id), None)
        if cited is not None:
            return cited if needle in _normalize(cited.text) else None
    return next((message for message in window.messages if needle in _normalize(message.text)), None)


def asks_to_remember(text: str) -> bool:
    """Whether a line asks the robot, in so many words, to keep something: the
    only consent a sensitive fact is written on (RFC 6.3). A refusal ("don't
    remember my diagnosis") and a question about the robot's memory ("do you
    remember my name?") are the opposite of consent, not weak forms of it."""
    lowered = text.lower().replace("’", "'").strip()
    if not lowered or lowered.endswith("?") or _NOT_REMEMBER.search(lowered) or _RECALLING.search(lowered):
        return False
    return _REMEMBER.search(lowered) is not None


def _consented(window: Window, tag: str, quoted: Utterance) -> bool:
    """Whether the person the sensitive fact is about asked for it to be kept,
    in the line it was quoted from or the one beside it. Anyone else's "remember
    this", and one anywhere else in the window, are not their consent."""
    index = next((at for at, message in enumerate(window.messages) if message is quoted), -1)
    if index < 0:
        return False
    return any(
        message.speaker is Speaker.USER and message.view(tag) is not None and asks_to_remember(message.text)
        for message in window.messages[max(index - 1, 0) : index + 2]
    )


def _heard_before(store: PeopleStore, person_id: str, name: str) -> bool:
    """Whether this name is already waiting as a candidate on this person."""
    profile = store.profile(person_id)
    return profile is not None and any(candidate.name == name for candidate in profile.name_candidates)


def _fact_ids(store: PeopleStore, person_id: str) -> set[str]:
    profile = store.profile(person_id)
    return {fact.id for fact in profile.facts} if profile is not None else set()


def _importance(kind: FactKind, confidence: float) -> float:
    weight = {
        FactKind.IDENTITY: 0.9,
        FactKind.RELATIONSHIP: 0.8,
        FactKind.REQUEST: 0.8,
        FactKind.PREFERENCE: 0.7,
        FactKind.ROUTINE: 0.6,
        FactKind.BIOGRAPHY: 0.6,
        FactKind.SENSITIVE: 0.5,
        FactKind.APPEARANCE: 0.2,
    }.get(kind, 0.5)
    return round(weight * max(0.1, min(confidence, 1.0)), 3)


# ------------------------------------------------------------ offline queue


class WindowQueue:
    """Windows waiting for Gemini to come back, bounded to one hour on disk.

    One JSON line per window, rewritten whole on every change: a few hundred
    small records, and a torn file would cost more than the rewrite does.
    """

    def __init__(self, path: Path, *, horizon_sec: float = QUEUE_HORIZON_SEC, limit: int = QUEUE_MAX_WINDOWS):
        self._path = path
        self._horizon_sec = horizon_sec
        self._limit = limit
        self._windows: list[Window] = _read_queue(path)

    def push(self, window: Window, now: float) -> None:
        self._windows.append(window)
        self._prune(now)
        self._commit()

    def expire(self, now: float) -> None:
        """Drop the windows past the horizon from disk as well: the hour they
        are promised has to pass whether or not anything is spending them."""
        if self._prune(now):
            self._commit()

    def pending(self, now: float) -> list[Window]:
        self.expire(now)
        return list(self._windows)

    def pop(self, window: Window) -> None:
        if window in self._windows:
            self._windows.remove(window)
            self._commit()

    def clear(self) -> None:
        self._windows = []
        self._commit()

    def forget(self, person_id: str) -> int:
        """Drop every queued window the person was in view for, and say how many.
        Deleting someone has to reach the work still in flight, or the outage
        that queued it sends their transcript to Gemini when it clears."""
        kept = [window for window in self._windows if person_id not in window.person_ids()]
        dropped = len(self._windows) - len(kept)
        if dropped:
            self._windows = kept
            self._commit()
        return dropped

    def __len__(self) -> int:
        return len(self._windows)

    def _prune(self, now: float) -> bool:
        """Whether anything was dropped — pruning only ever removes, so the
        count is the whole answer, and it tells the caller to rewrite the file."""
        kept = [window for window in self._windows if now - window.closed <= self._horizon_sec][-self._limit :]
        if len(kept) == len(self._windows):
            return False
        self._windows = kept
        return True

    def _commit(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
        tmp = self._path.with_name(self._path.name + ".tmp")
        tmp.write_text("".join(json.dumps(window_to_dict(window)) + "\n" for window in self._windows), encoding="utf-8")
        os.replace(tmp, self._path)


def window_to_dict(window: Window) -> dict:
    return {
        "messages": [
            {
                "id": message.id,
                "stamp": message.stamp,
                "speaker": str(message.speaker),
                "text": message.text,
                "in_view": [
                    {
                        "tag": view.tag,
                        "state": str(view.state),
                        "person_id": view.person_id,
                        "name": view.name,
                        "enrolling": view.enrolling,
                    }
                    for view in message.in_view
                ],
            }
            for message in window.messages
        ]
    }


def window_from_dict(data: dict) -> Window:
    messages = []
    for entry in _list(data.get("messages")):
        message = _dict(entry)
        messages.append(
            Utterance(
                id=str(message.get("id", "")),
                stamp=float(message.get("stamp") or 0.0),
                speaker=Speaker.ROBOT if message.get("speaker") == Speaker.ROBOT else Speaker.USER,
                text=str(message.get("text", "")),
                in_view=tuple(
                    TagView(
                        tag=str(_dict(view).get("tag", "")),
                        state=_state(_dict(view).get("state")),
                        person_id=_optional_str(_dict(view).get("person_id")),
                        name=_optional_str(_dict(view).get("name")),
                        enrolling=bool(_dict(view).get("enrolling", False)),
                    )
                    for view in _list(message.get("in_view"))
                ),
            )
        )
    return Window(tuple(messages))


def _read_queue(path: Path) -> list[Window]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    windows: list[Window] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue  # one torn line must not cost the rest of the queue
        if isinstance(data, dict):
            windows.append(window_from_dict(data))
    return windows


# ------------------------------------------------------------- deep recall

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
    data = _response_json(response)
    if data is None or not data.get("found"):
        return ""
    return str(data.get("answer") or "").strip()


# --------------------------------------------------------------- the worker


class Scribe:
    """Windows in, changes out. The node feeds it chat messages and ticks it;
    every call to Gemini happens on the caller's thread, so the node runs it on
    a worker and never on its executor."""

    def __init__(
        self,
        store: PeopleStore,
        transport: Transport | None,
        *,
        model: str,
        queue_path: Path,
        timeout: float = SCRIBE_TIMEOUT_SEC,
    ):
        self._store = store
        self._transport = transport
        self._model = model
        self._timeout = timeout
        self._buffer = WindowBuffer()
        self._queue = WindowQueue(queue_path)
        self._forgotten: set[str] = set()
        self.last_error = ""

    @property
    def queued(self) -> int:
        return len(self._queue)

    def observe(self, message: Utterance, now: float) -> list[Change]:
        """Buffer one chat message; a window that fills up is spent at once.
        Nothing is buffered while collection is off — that switch is what it
        means (RFC section 10) — and the flag is read here rather than where the
        message was queued, because it may have flipped in between."""
        if not self._store.collection_enabled():
            return []
        live = line_without_revoked(message, self._revoked)
        if live is None:
            return []
        window = self._buffer.add(live)
        return [] if window is None else self.process(window, now)

    def tick(self, now: float) -> list[Change]:
        """Close an idle window and drain whatever an outage left queued."""
        if not self._store.collection_enabled():
            self._buffer.clear()
            self._queue.expire(now)  # the hour on disk runs whether or not it is being spent
            return []
        changes = self.drain(now)
        window = self._buffer.due(now)
        if window is not None:
            changes.extend(self.process(window, now))
        return changes

    def process(self, window: Window, now: float) -> list[Change]:
        """One window, end to end. A window Gemini could not take queues on
        disk; recognition never depends on any of this."""
        window = self._live(window)
        if not window.messages or not self._store.collection_enabled():
            return []
        try:
            output = self._call(window)
        except Exception as error:  # noqa: BLE001 — any transport failure queues the window for the drain
            self.last_error = repr(error)
            self._queue.push(window, now)
            return []
        return [] if output is None else self._commit(output, window, now)

    def drain(self, now: float) -> list[Change]:
        """Spend the queued windows, oldest first, stopping at the first
        failure — the connection is still down and the rest can wait."""
        if not self._store.collection_enabled():
            return []
        changes: list[Change] = []
        for queued in self._queue.pending(now):
            window = self._live(queued)
            if not window.messages:
                self._queue.pop(queued)
                continue
            try:
                output = self._call(window)
            except Exception as error:  # noqa: BLE001 — the rest of the queue waits for the connection
                self.last_error = repr(error)
                break
            self._queue.pop(queued)
            if output is not None:
                changes.extend(self._commit(output, window, now))
        return changes

    def forget(self, person_id: str) -> None:
        """Everything about a forgotten person that has not been written yet:
        the open window, the queue an outage filled, and every call and write
        this thread has not made yet (RFC section 10)."""
        self._forgotten.add(person_id)
        self._buffer.forget(person_id)
        self._queue.forget(person_id)

    def recall(self, person_id: str, question: str) -> str:
        """Deep recall over one person's memory; empty when it adds nothing."""
        profile = self._store.profile(person_id)
        if profile is None or self._transport is None or self._revoked(person_id):
            return ""
        try:
            response = self._transport(
                GENERATE_PATH.format(model=self._model),
                recall_request(profile_to_dict(profile), question),
                RECALL_TIMEOUT_SEC,
            )
        except Exception as error:  # noqa: BLE001 — a failed recall is silence, never a broken turn
            self.last_error = repr(error)
            return ""
        return parse_recall(response)

    def _commit(self, output: ScribeOutput, window: Window, now: float) -> list[Change]:
        """The proposal turned into writes, gated a second time: a forget that
        landed during the Gemini call takes back what that call was about to
        write."""
        window = self._live(window)
        return apply(output, window, self._store, now) if window.messages else []

    def _live(self, window: Window) -> Window:
        return without_revoked(window, self._revoked)

    def _revoked(self, person_id: str | None) -> bool:
        """Whether a forget has taken this person back (RFC section 10). Both
        answers count: the store's tombstone is the durable one, and the set
        holds the forgets this scribe was handed directly."""
        if person_id is None:
            return False
        return person_id in self._forgotten or self._store.is_tombstoned(person_id)

    def _call(self, window: Window) -> ScribeOutput | None:
        if self._transport is None:
            raise RuntimeError("no Gemini transport configured")
        body = build_request(window, self._context_facts(window))
        output = parse_output(self._transport(GENERATE_PATH.format(model=self._model), body, self._timeout))
        if output is None:
            self.last_error = "unreadable scribe answer"
        return output

    def _context_facts(self, window: Window) -> dict[str, list[Fact]]:
        facts: dict[str, list[Fact]] = {}
        for tag, view in window.views().items():
            profile = self._store.profile(view.person_id) if view.person_id else None
            if profile is not None:
                facts[tag] = [fact for fact in profile.facts if fact.superseded_by is None]
        return facts


# ------------------------------------------------------------------ parsing


def _fact(entry: object, kind: FactKind | None = None) -> ScribeFact:
    data = _dict(entry)
    return ScribeFact(
        who=str(data.get("who", "")),
        text=str(data.get("text", "")),
        kind=kind or _member(FactKind, data.get("kind"), FactKind.BIOGRAPHY),
        confidence=_float(data.get("confidence"), 0.5),
        attribution=_member(Attribution, data.get("attribution"), Attribution.UNCERTAIN),
        quote=str(data.get("quote", "")),
        utterance=str(data.get("utterance", "")),
        supersedes=str(data.get("supersedes") or ""),
    )


def _name(entry: object) -> ScribeName:
    data = _dict(entry)
    return ScribeName(
        who=str(data.get("who", "")),
        name=str(data.get("name", "")).strip(),
        confidence=_float(data.get("confidence"), 0.0),
        quote=str(data.get("quote", "")),
        utterance=str(data.get("utterance", "")),
        introduction=_member(Introduction, data.get("introduction"), Introduction.OTHER),
        referent=str(data.get("referent") or ""),
        correction=bool(data.get("correction", False)),
    )


def _loop(entry: object) -> ScribeLoop:
    data = _dict(entry)
    return ScribeLoop(
        who=str(data.get("who", "")),
        text=str(data.get("text", "")),
        due=str(data.get("due")) if data.get("due") else None,
        quote=str(data.get("quote", "")),
        utterance=str(data.get("utterance", "")),
    )


def _response_json(response: dict) -> dict | None:
    try:
        parts = response["candidates"][0]["content"]["parts"]
        text = next(part["text"] for part in parts if part.get("text") and not part.get("thought"))
        data = json.loads(text)
    except (KeyError, IndexError, TypeError, ValueError, StopIteration):
        return None
    return data if isinstance(data, dict) else None


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower().replace("’", "'")).strip()


def _state(value: object) -> IdentityState:
    return _member(IdentityState, value, IdentityState.UNKNOWN)


def _member(enum: type[_E], value: object, default: _E) -> _E:
    try:
        return enum(str(value))
    except ValueError:
        return default


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _float(value: object, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)
