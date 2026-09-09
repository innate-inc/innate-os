# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The rule set that decides what one window's proposal is actually allowed to
write (RFC 6.3, docs/rfc/people-memory.md in innate-jetson): every item checked
against the line it was quoted from and who was in view for it, the three ways
a name may commit, the consent a sensitive fact needs, and the appearance note
that supersedes today's earlier one. A refusal is a rejected change carrying
its reason, never a silent drop. PURE module: the store is the caller's."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from brain_client.common.enums import StrEnum
from brain_client.people.memory import Attribution, ConsentPath, FactKind, FactSource
from brain_client.people.scribe_output import Introduction
from brain_client.people.transcript import Speaker

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from brain_client.people.scribe_output import ScribeFact, ScribeLoop, ScribeName, ScribeNote, ScribeOutput
    from brain_client.people.store import PeopleStore
    from brain_client.people.transcript import TagView, Utterance, Window

_REMEMBER = re.compile(
    r"\b(remember|memorise|memorize|don'?t forget|do not forget|make a note|note that|keep in mind|"
    r"write (this|that) down)\b"
)
_NOT_REMEMBER = re.compile(
    r"\b((do|did|does|would|will|can|could)\s?(not|n'?t)|never|stop|no need to|rather not)"
    r"\s+(?:\w+\s+){0,3}?(remember|note|keep|memoris|memoriz|writ|jot|record)"
)
_RECALLING = re.compile(
    r"\bremember when\b|\b(do|did|does|can|could|would|will|have)(n'?t)?\s+(you|we|i|they|he|she)\s+"
    r"(?:\w+\s+){0,2}?(remember|recall)\b"
)
_COUNTS = {2: "two", 3: "three", 4: "four", 5: "five"}


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


@dataclass(frozen=True)
class Change:
    """One thing the window changed (or refused to change)."""

    kind: ChangeKind
    tag: str
    person_id: str | None = None
    text: str = ""
    reason: str = ""
    record_id: str = ""


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
    quoted = _quoted_message(window, fact.utterance, fact.quote)
    if quoted is None:
        return Change(
            ChangeKind.REJECTED, fact.who, view.person_id, fact.text, reason="the quote is not in this window"
        )
    # RFC 6.3: a fact is written only if its subject was in view during the line
    # it was quoted from. Writing it anyway puts the room's words on one
    # person's record, and lets them supersede a fact that was really theirs.
    subject = quoted.view(fact.who)
    if subject is None:
        return Change(
            ChangeKind.REJECTED, fact.who, view.person_id, fact.text, reason="not in view during the quoted line"
        )
    # The tag as it stood on that line, not the window's latest: the resolver
    # may switch a committed track to another person inside one window.
    person_id = subject.person_id
    if person_id is None:
        return Change(ChangeKind.REJECTED, fact.who, None, fact.text, reason="no tracked person for that tag")
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
        return _candidate(name, referents, quoted, store, now, reason="the words are not an introduction")
    if quoted is None:
        # Without the line it came from there is no way to check who was in
        # view when it was said, and a name is never committed on trust.
        return _candidate(name, referents, quoted, store, now, reason="the quoted words are not in this window")
    if len(referents) > 1:
        return _candidate(name, referents, quoted, store, now, reason="more than one person could be the referent")
    view = referents[0]
    if view.person_id is None:
        return Change(ChangeKind.REJECTED, view.tag, None, name.name, reason="no tracked person for that tag")
    person_id = view.person_id
    if not view.nameable:
        return _candidate(name, referents, quoted, store, now, reason="the track is neither confirmed nor enrolling")
    # RFC 6.3: the robot's own use of a name is reinforcing evidence, never the
    # trigger — it may only settle a name a person was already heard to give.
    if quoted.speaker is Speaker.ROBOT and not _heard_before(store, person_id, name.name):
        return _candidate(
            name, referents, quoted, store, now, reason="only the robot said the name, and nobody else has"
        )
    profile = store.profile(person_id)
    on_file = profile.names.preferred if profile is not None else None
    consent = profile.consent.how if profile is not None else None
    correcting = bool(on_file) and on_file != name.name
    if correcting and not (name.correction and name.introduction in (Introduction.SELF, Introduction.OWNER)):
        return _candidate(
            name,
            referents,
            quoted,
            store,
            now,
            reason=f"another name is on file ({on_file})",
            hint=f"heard '{name.quote}' but {view.tag} is already {on_file}; ask if that is a correction",
        )
    # RFC 6.3: the owner's app entry always wins, so a conversation may talk
    # over it only when the owner is the one talking.
    if correcting and consent == ConsentPath.APP and name.introduction is not Introduction.OWNER:
        return _candidate(
            name,
            referents,
            quoted,
            store,
            now,
            reason=f"the app named {view.tag} {on_file}",
            hint=f"heard '{name.quote}' but the app named {view.tag} {on_file}; the owner changes that in the app",
        )
    # A name heard as it already stands keeps the path it was set on: restamping
    # an app entry as conversational would open the gate above to the next one.
    source = consent if consent and on_file == name.name else str(ConsentPath.CONVERSATION)
    if not store.rename(person_id, name.name, source, now=now):
        return Change(ChangeKind.REJECTED, view.tag, person_id, name.name, reason="the store refused the name")
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
    quoted: Utterance | None,
    store: PeopleStore,
    now: float,
    *,
    reason: str,
    hint: str | None = None,
) -> Change:
    """A name that did not meet the commit rules: surfaced as a question the
    agent may ask, and filed against every person it could belong to — but only
    when a person said it. A candidate carries no speaker, so one filed from the
    robot's own line reads back as the introduction :func:`_heard_before` looks
    for, and the robot's next mention commits a name nobody ever gave."""
    if quoted is not None and quoted.speaker is Speaker.USER:
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
    # Same rule as a fact, and for the same reason: an uncited promise — the
    # schema asks for the words and the line — lands on the named person with
    # nothing to check it against (RFC 6.3).
    quoted = _quoted_message(window, loop.utterance, loop.quote)
    if quoted is None:
        return Change(
            ChangeKind.REJECTED, loop.who, view.person_id, loop.text, reason="the quote is not in this window"
        )
    subject = quoted.view(loop.who)
    if subject is None:
        return Change(
            ChangeKind.REJECTED, loop.who, view.person_id, loop.text, reason="not in view during the quoted line"
        )
    person_id = subject.person_id
    if person_id is None:
        return Change(ChangeKind.REJECTED, loop.who, None, loop.text, reason="no tracked person for that tag")
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
    record = _write_appearance(store, person_id, note.text, now)
    if record is None:
        return Change(ChangeKind.REJECTED, note.who, person_id, note.text, reason="the store refused the note")
    return Change(ChangeKind.APPEARANCE, note.who, person_id, note.text, record_id=record)


def _write_appearance(store: PeopleStore, person_id: str, text: str, now: float) -> str | None:
    """Today's look, superseding the one it replaces: a long conversation is
    many windows, and each one would otherwise file the same jacket again."""
    previous = _open_appearance(store, person_id)
    source = FactSource(stamp=now)
    if previous is None:
        return store.add_fact(
            person_id,
            text,
            FactKind.APPEARANCE,
            now=now,
            attribution=Attribution.ROBOT,
            source=source,
            confidence=0.5,
            importance=0.2,
        )
    return store.supersede_fact(
        person_id,
        previous,
        text,
        FactKind.APPEARANCE,
        now=now,
        attribution=Attribution.ROBOT,
        source=source,
        confidence=0.5,
        importance=0.2,
    )


def _open_appearance(store: PeopleStore, person_id: str) -> str | None:
    profile = store.profile(person_id)
    if profile is None:
        return None
    live = (fact for fact in reversed(profile.facts) if fact.superseded_by is None)
    return next((fact.id for fact in live if fact.kind is FactKind.APPEARANCE), None)


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
            return cited if _quotes(cited.text, needle) else None
    return next((message for message in window.messages if _quotes(message.text, needle)), None)


def _quotes(text: str, needle: str) -> bool:
    """Whether the line really carries those words. On word bounds: a bare
    substring makes "Ana" a quote of "do you like bananas"."""
    return re.search(rf"\b{re.escape(needle)}\b", _normalize(text)) is not None


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
    return any(_asked_alone(message, tag) for message in window.messages[max(index - 1, 0) : index + 2])


def _asked_alone(message: Utterance, tag: str) -> bool:
    """A line asking the robot to remember, with the subject as the only person
    in view. An utterance records who was *visible*, never who spoke, so with
    somebody else in the room "Alice is on chemo, remember that" is their
    request and not hers — the same one-plausible-referent rule the passive
    naming path uses (RFC 6.3)."""
    return (
        message.speaker is Speaker.USER
        and tuple(view.tag for view in message.in_view) == (tag,)
        and asks_to_remember(message.text)
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


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower().replace("’", "'")).strip()
