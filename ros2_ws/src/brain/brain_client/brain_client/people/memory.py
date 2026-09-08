# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The person memory: what the robot knows about someone, as opposed to how it
recognizes them (docs/rfc/people-memory.md section 6.2 in innate-jetson).

PURE module: no rclpy, no I/O. The roster half — face templates, outfits,
thumbnails — is visual identity and lives in :mod:`brain_client.people.store`;
these are the records a conversation writes: a profile, dated facts with
provenance, episodes, and open loops. Stamps are epoch seconds.

A fact is never edited in place. A statement that updates an older one
supersedes it (:func:`supersede`), so provenance and "first confirmed" survive
the correction and the audit log can be read back against the profile. Ranking
is the one piece of judgement here: importance, recency and lexical overlap
with what is being talked about decide what the per-turn block has room for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, TypeVar

from brain_client.common.enums import StrEnum

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

PROFILE_VERSION = 1
FACT_TEXT_LIMIT = 200
MAX_DIGEST_FACTS = 40
MAX_DIGEST_EPISODES = 3
EPISODE_IDLE_SEC = 300.0
"""An episode closes after this long unseen; the next sighting is a new encounter."""

_RECENCY_HALF_LIFE_SEC = 30 * 86400.0
_CONTEXT_BONUS = 0.5  # a fact the conversation is already about outranks a fresher one
_E = TypeVar("_E", bound=StrEnum)
_WORD = re.compile(r"[a-z0-9']+")
_STOPWORDS = frozenset(
    "a an and are as at be by do does did for from has have he her him his i if in is it its me my "
    "no not of on or our she that the their them they this to us was we were what when where who "
    "will with you your".split()
)


class FactKind(StrEnum):
    """Wire-visible in person.json and in the snapshot digest."""

    IDENTITY = "identity"
    PREFERENCE = "preference"
    BIOGRAPHY = "biography"
    RELATIONSHIP = "relationship"
    ROUTINE = "routine"
    REQUEST = "request"
    SENSITIVE = "sensitive"
    APPEARANCE = "appearance"


class Attribution(StrEnum):
    """Where a fact came from. ``UNCERTAIN`` is the scribe's "several people were
    in view and the transcript does not say which" — deep recall only."""

    SELF = "self"
    THIRD_PARTY = "third_party"
    ROBOT = "robot"
    OWNER = "owner"
    UNCERTAIN = "uncertain"


class Relationship(StrEnum):
    OWNER = "owner"
    HOUSEHOLD = "household"
    REGULAR = "regular"
    GUEST = "guest"
    UNKNOWN = "unknown"


class ConsentPath(StrEnum):
    """How a name was learned; stored with the profile as the consent basis."""

    CONVERSATION = "conversation"
    APP = "app"
    SKILL = "skill"


@dataclass(frozen=True)
class FactSource:
    utterance_id: str = ""
    stamp: float = 0.0
    quote: str = ""
    speaker_tag: str = ""


@dataclass(frozen=True)
class Fact:
    id: str
    text: str
    kind: FactKind = FactKind.BIOGRAPHY
    confidence: float = 0.5
    attribution: Attribution = Attribution.UNCERTAIN
    source: FactSource = field(default_factory=FactSource)
    first_confirmed: float = 0.0
    last_confirmed: float = 0.0
    superseded_by: str | None = None
    importance: float = 0.5


@dataclass(frozen=True)
class Episode:
    id: str
    start: float
    end: float | None = None
    map: str | None = None
    x: float | None = None
    y: float | None = None
    present: tuple[str, ...] = ()
    summary: str = ""
    events: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpenLoop:
    id: str
    text: str
    created: float = 0.0
    due: str | None = None
    source: str = ""
    done: bool = False


@dataclass(frozen=True)
class NameCandidate:
    name: str
    stamp: float = 0.0
    quote: str = ""
    tag: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class Names:
    preferred: str | None = None
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class Consent:
    how: str | None = None
    stamp: float = 0.0


@dataclass(frozen=True)
class Description:
    text: str
    stamp: float = 0.0
    thumbnail_id: str | None = None


@dataclass(frozen=True)
class LastSeen:
    stamp: float
    map: str | None = None
    x: float | None = None
    y: float | None = None


@dataclass(frozen=True)
class Profile:
    """One person's memory, as person.json holds it."""

    id: str
    names: Names = field(default_factory=Names)
    relationship: Relationship = Relationship.UNKNOWN
    description: Description | None = None
    consent: Consent = field(default_factory=Consent)
    created: float = 0.0
    last_seen: LastSeen | None = None
    encounters: int = 0
    facts: tuple[Fact, ...] = ()
    episodes: tuple[Episode, ...] = ()
    open_loops: tuple[OpenLoop, ...] = ()
    name_candidates: tuple[NameCandidate, ...] = ()
    retention_days: float | None = None

    @property
    def named(self) -> bool:
        return bool(self.names.preferred)

    @property
    def name(self) -> str | None:
        return self.names.preferred


# ------------------------------------------------------------------- ranking


def words(text: str) -> set[str]:
    """Content words of a phrase, for lexical context matching."""
    return {word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS and len(word) > 1}


def recency_weight(age_sec: float) -> float:
    """1.0 for a fact confirmed now, halving every 30 days; never negative."""
    return 0.5 ** (max(age_sec, 0.0) / _RECENCY_HALF_LIFE_SEC)


def fact_score(fact: Fact, now: float, context_words: Iterable[str] = ()) -> float:
    """importance x recency decay x lexical context match, per RFC 6.4."""
    context = set(context_words)
    overlap = len(words(fact.text) & context) if context else 0
    match = 1.0 + _CONTEXT_BONUS * min(overlap, 3)
    return fact.importance * recency_weight(now - fact.last_confirmed) * match


def surfaceable(fact: Fact) -> bool:
    """Whether a fact may ride the per-turn block. Sensitive facts are deep
    recall only, appearance notes belong to the encounter, and an unattributed
    fact must never be asserted about the person in front of the robot."""
    return (
        fact.superseded_by is None
        and fact.kind not in (FactKind.SENSITIVE, FactKind.APPEARANCE)
        and fact.attribution is not Attribution.UNCERTAIN
    )


def rank_facts(facts: Sequence[Fact], now: float, context_words: Iterable[str] = ()) -> list[Fact]:
    """Live, surfaceable facts, best first."""
    context = set(context_words)
    live = [fact for fact in facts if surfaceable(fact)]
    return sorted(live, key=lambda fact: (-fact_score(fact, now, context), -fact.last_confirmed, fact.id))


# ------------------------------------------------------------- record edits


def next_sequence_id(prefix: str, existing: Iterable[str]) -> str:
    """``f_01``, ``o_02``, ``e_03`` — stable, human-readable, never reused
    within a profile even after a delete."""
    taken = {str(value) for value in existing}
    number = 1
    while f"{prefix}_{number:02d}" in taken:
        number += 1
    return f"{prefix}_{number:02d}"


def supersede(facts: Sequence[Fact], fact_id: str, replacement: Fact) -> tuple[Fact, ...]:
    """Point an outdated fact at its replacement and append it, keeping the
    original's first_confirmed. An unknown id just appends."""
    old = next((fact for fact in facts if fact.id == fact_id), None)
    if old is not None:
        replacement = replace(replacement, first_confirmed=old.first_confirmed or replacement.first_confirmed)
    updated = tuple(replace(fact, superseded_by=replacement.id) if fact.id == fact_id else fact for fact in facts)
    return (*updated, replacement)


def open_episode(episodes: Sequence[Episode], episode: Episode) -> tuple[Episode, ...]:
    """Start an episode, closing any that was left open (a node restart, a
    track that ended without a sighting)."""
    closed = tuple(replace(existing, end=episode.start) if existing.end is None else existing for existing in episodes)
    return (*closed, episode)


def close_episode(episodes: Sequence[Episode], end: float, summary: str = "") -> tuple[Episode, ...]:
    """Close the open episode; a summary replaces the placeholder one."""
    open_one = latest_open(episodes)
    if open_one is None:
        return tuple(episodes)
    closed = replace(open_one, end=end, summary=summary or open_one.summary)
    return tuple(closed if episode.id == open_one.id else episode for episode in episodes)


def latest_open(episodes: Sequence[Episode]) -> Episode | None:
    return next((episode for episode in reversed(episodes) if episode.end is None), None)


# ------------------------------------------------------------ serialization


def profile_to_dict(profile: Profile) -> dict:
    """person.json's payload. Written by the store; the shape is the RFC's."""
    return {
        "version": PROFILE_VERSION,
        "id": profile.id,
        "names": {"preferred": profile.names.preferred, "aliases": list(profile.names.aliases)},
        "relationship": str(profile.relationship),
        "description": (
            None
            if profile.description is None
            else {
                "text": profile.description.text,
                "stamp": profile.description.stamp,
                "thumbnail_id": profile.description.thumbnail_id,
            }
        ),
        "consent": {"how": profile.consent.how, "stamp": profile.consent.stamp},
        "created": profile.created,
        "last_seen": (
            None
            if profile.last_seen is None
            else {
                "stamp": profile.last_seen.stamp,
                "map": profile.last_seen.map,
                "x": profile.last_seen.x,
                "y": profile.last_seen.y,
            }
        ),
        "encounters": profile.encounters,
        "facts": [
            {
                "id": fact.id,
                "text": fact.text,
                "kind": str(fact.kind),
                "confidence": fact.confidence,
                "attribution": str(fact.attribution),
                "source": {
                    "utterance_id": fact.source.utterance_id,
                    "stamp": fact.source.stamp,
                    "quote": fact.source.quote,
                    "speaker_tag": fact.source.speaker_tag,
                },
                "first_confirmed": fact.first_confirmed,
                "last_confirmed": fact.last_confirmed,
                "superseded_by": fact.superseded_by,
                "importance": fact.importance,
            }
            for fact in profile.facts
        ],
        "episodes": [
            {
                "id": episode.id,
                "start": episode.start,
                "end": episode.end,
                "map": episode.map,
                "x": episode.x,
                "y": episode.y,
                "present": list(episode.present),
                "summary": episode.summary,
                "events": list(episode.events),
            }
            for episode in profile.episodes
        ],
        "open_loops": [
            {
                "id": loop.id,
                "text": loop.text,
                "created": loop.created,
                "due": loop.due,
                "source": loop.source,
                "done": loop.done,
            }
            for loop in profile.open_loops
        ],
        "name_candidates": [
            {
                "name": candidate.name,
                "stamp": candidate.stamp,
                "quote": candidate.quote,
                "tag": candidate.tag,
                "confidence": candidate.confidence,
            }
            for candidate in profile.name_candidates
        ],
        "retention_days": profile.retention_days,
    }


def profile_from_dict(data: dict) -> Profile:
    """Parse person.json. Tolerant of missing keys — a profile written by an
    older build must load rather than take the person out of the roster."""
    names = _dict(data.get("names"))
    description = _dict(data.get("description")) if data.get("description") else None
    consent = _dict(data.get("consent"))
    last_seen = _dict(data.get("last_seen")) if data.get("last_seen") else None
    return Profile(
        id=str(data.get("id", "")),
        names=Names(
            preferred=_optional_str(names.get("preferred")),
            aliases=tuple(str(alias) for alias in _list(names.get("aliases"))),
        ),
        relationship=_member(Relationship, data.get("relationship"), Relationship.UNKNOWN),
        description=(
            None
            if description is None
            else Description(
                text=str(description.get("text", "")),
                stamp=_float(description.get("stamp")),
                thumbnail_id=_optional_str(description.get("thumbnail_id")),
            )
        ),
        consent=Consent(how=_optional_str(consent.get("how")), stamp=_float(consent.get("stamp"))),
        created=_float(data.get("created")),
        last_seen=(
            None
            if last_seen is None
            else LastSeen(
                stamp=_float(last_seen.get("stamp")),
                map=_optional_str(last_seen.get("map")),
                x=_optional_float(last_seen.get("x")),
                y=_optional_float(last_seen.get("y")),
            )
        ),
        encounters=int(_float(data.get("encounters"))),
        facts=tuple(_fact_from_dict(_dict(entry)) for entry in _list(data.get("facts"))),
        episodes=tuple(_episode_from_dict(_dict(entry)) for entry in _list(data.get("episodes"))),
        open_loops=tuple(_loop_from_dict(_dict(entry)) for entry in _list(data.get("open_loops"))),
        name_candidates=tuple(_candidate_from_dict(_dict(entry)) for entry in _list(data.get("name_candidates"))),
        retention_days=_optional_float(data.get("retention_days")),
    )


def _fact_from_dict(data: dict) -> Fact:
    source = _dict(data.get("source"))
    return Fact(
        id=str(data.get("id", "")),
        text=str(data.get("text", "")),
        kind=_member(FactKind, data.get("kind"), FactKind.BIOGRAPHY),
        confidence=_float(data.get("confidence")),
        attribution=_member(Attribution, data.get("attribution"), Attribution.UNCERTAIN),
        source=FactSource(
            utterance_id=str(source.get("utterance_id", "")),
            stamp=_float(source.get("stamp")),
            quote=str(source.get("quote", "")),
            speaker_tag=str(source.get("speaker_tag", "")),
        ),
        first_confirmed=_float(data.get("first_confirmed")),
        last_confirmed=_float(data.get("last_confirmed")),
        superseded_by=_optional_str(data.get("superseded_by")),
        importance=_float(data.get("importance")),
    )


def _episode_from_dict(data: dict) -> Episode:
    return Episode(
        id=str(data.get("id", "")),
        start=_float(data.get("start")),
        end=_optional_float(data.get("end")),
        map=_optional_str(data.get("map")),
        x=_optional_float(data.get("x")),
        y=_optional_float(data.get("y")),
        present=tuple(str(tag) for tag in _list(data.get("present"))),
        summary=str(data.get("summary", "")),
        events=tuple(str(event) for event in _list(data.get("events"))),
    )


def _loop_from_dict(data: dict) -> OpenLoop:
    return OpenLoop(
        id=str(data.get("id", "")),
        text=str(data.get("text", "")),
        created=_float(data.get("created")),
        due=_optional_str(data.get("due")),
        source=str(data.get("source", "")),
        done=bool(data.get("done", False)),
    )


def _candidate_from_dict(data: dict) -> NameCandidate:
    return NameCandidate(
        name=str(data.get("name", "")),
        stamp=_float(data.get("stamp")),
        quote=str(data.get("quote", "")),
        tag=str(data.get("tag", "")),
        confidence=_float(data.get("confidence")),
    )


def _member(enum: type[_E], value: object, default: _E) -> _E:
    try:
        return enum(str(value))
    except ValueError:
        return default


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _float(value: object) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _optional_float(value: object) -> float | None:
    return None if value is None else _float(value)


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)
