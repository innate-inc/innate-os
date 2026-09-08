# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The People block of a turn input: who is in view and what memory says.

PURE module: no rclpy, no cv2, no I/O. The people node publishes a memory
*digest* per visible person on ``/brain/people`` (state, description, up to 40
facts, every open loop, episodes, last seen); this file is the second half of
that split (docs/rfc/people-memory.md 6.4) — it selects and words the few lines
worth a turn's tokens, ranking facts by importance x recency x lexical overlap
with what is being said right now, so "likes pasta" surfaces when dinner comes
up and stays out of the way otherwise.

The block is information, never instruction: nothing it says implies a tool
call. Accumulated confidence is never printed (a cosine is not a percentage,
RFC 5.2) and facts the scribe marked ``sensitive`` never reach it.
"""

from __future__ import annotations

import math
import re
from datetime import datetime

from brain_client.people.types import (
    AttentionDict,
    Evidence,
    FactDict,
    IdentityState,
    LastSeenDict,
    OpenLoopDict,
    PeopleSnapshotDict,
    PersonDigestDict,
    PersonInViewDict,
    RecentPersonDict,
)

_PERSON_BUDGET_TOKENS = 120
_BLOCK_BUDGET_TOKENS = 400
_TOKENS_PER_WORD = 1.3  # words -> Gemini tokens, close enough to spend a budget against

_MAX_FACTS = 4
_MAX_RECENT = 4
_STALE_SNAPSHOT_SEC = 10.0  # older, and "in view" is a claim about a scene that has moved on
_LAST_SEEN_QUIET_SEC = 60.0  # they are standing here: "last seen just now" is noise
_RECENCY_HALF_LIFE_SEC = 30 * 86400.0
_RECENCY_FLOOR = 0.3  # an old fact stays rankable, so importance can still win
_CONTEXT_WEIGHT = 1.5  # a fact the conversation is about outranks one twice as important
_SENSITIVE = "sensitive"

_EVIDENCE_WORDS = {
    Evidence.FACE: "face",
    Evidence.OUTFIT: "clothes",
    Evidence.CONTINUITY: "continuity",
    Evidence.HEIGHT: "height",
    Evidence.LEGS: "legs",
}

_STOP_WORDS = frozenset(
    "the and but for you your yours are was were with that this these those have has had not can could would should "
    "will just about from what when where which who whom how why all any some there their they them his her hers its "
    "into over under then than out off own too very says said user robot skill event update running now here".split()
)


def render(
    snapshot: PeopleSnapshotDict,
    events_text: list[str],
    conversation: list[tuple[str, str]],
    now: float,
    boxes_drawn: bool,
) -> str | None:
    """The People block, or None when there is nobody to say anything about.

    ``conversation`` is the last few (speaker, text) exchanges and
    ``events_text`` this turn's stimuli; together they are the context facts
    are ranked against. ``boxes_drawn`` False means the head frame carries no
    overlay this turn, so the block must not let the model believe a tag is
    marked on the picture.
    """
    people = _in_view(snapshot, now)
    context = _context_words(conversation, events_text)
    lines = _people_lines(people, now, context, boxes_drawn)
    if people:
        lines += _attention_lines(snapshot)
    lines += _recent_lines(snapshot, now, people)
    if not lines:
        return None
    return "\n".join(lines)


def frame_stamp_ns(snapshot: PeopleSnapshotDict) -> int | None:
    """The ROS header stamp of the frame the engine measured, as an int — the
    wire carries it as a decimal string so JS consumers keep every digit."""
    raw = snapshot.get("frame_stamp_ns")
    if not isinstance(raw, str) or not raw.isdigit():
        return None
    return int(raw)


def _in_view(snapshot: PeopleSnapshotDict, now: float) -> list[PersonInViewDict]:
    """The snapshot's people, live ones first — empty once the snapshot is too
    old to describe the scene the frame shows."""
    if now - float(snapshot.get("stamp") or 0.0) > _STALE_SNAPSHOT_SEC:
        return []
    people = snapshot.get("people") or []
    return [p for p in people if not p.get("lost")] + [p for p in people if p.get("lost")]


def _people_lines(people: list[PersonInViewDict], now: float, context: set[str], boxes_drawn: bool) -> list[str]:
    """Every person's header, then their extras in priority order while the
    ~120-per-person and ~400-per-block token budgets hold."""
    if not people:
        return []
    header = "People in view:" if boxes_drawn else "People in view (positions not drawn this turn):"
    entries = [_person_lines(person, now, context) for person in people]
    kept = [[entry[0]] for entry in entries]
    spent = _tokens(header) + sum(_tokens(entry[0]) for entry in entries)
    for entry, block in zip(entries, kept, strict=True):
        person_spent = _tokens(entry[0])
        for extra in entry[1:]:
            cost = _tokens(extra)
            # Stopping is right, not skipping: the extras are in priority
            # order, so fitting a cheaper line would drop the more useful one.
            if person_spent + cost > _PERSON_BUDGET_TOKENS or spent + cost > _BLOCK_BUDGET_TOKENS:
                break
            block.append(extra)
            person_spent += cost
            spent += cost
    return [header] + [line for block in kept for line in block]


def _person_lines(person: PersonInViewDict, now: float, context: set[str]) -> list[str]:
    header = _header_line(person)
    lines = [header]
    facts = _facts_line(person, now, context, _PERSON_BUDGET_TOKENS - _tokens(header))
    if facts is not None:
        lines.append(facts)
    learned = _clean(person.get("learned"))
    if learned:
        lines.append(f"  learned just now: {_sentence(learned)}")
    hint = _clean(person.get("hint"))
    if hint:
        lines.append(f"  {_sentence(hint)}")
    history = _history_line(person, now)
    if history is not None:
        lines.append(history)
    episode = _episode_line(person)
    if episode is not None:
        lines.append(episode)
    return lines


def _header_line(person: PersonInViewDict) -> str:
    phrase = _identity_phrase(person)
    if person.get("lost"):
        phrase += ", just left view"
    line = f"- {phrase}."
    description = _clean(person.get("description"))
    if description:
        line += f" {_sentence(description)}"
    box = _box_text(person)
    if box:
        line += f" box {box}"
    return line


def _identity_phrase(person: PersonInViewDict) -> str:
    """RFC 5.1's five display states, worded for the model rather than the overlay."""
    tag = person.get("tag") or "P?"
    name = _clean(person.get("name"))
    state = person.get("state") or IdentityState.UNKNOWN
    evidence = _evidence_text(person)
    if state == IdentityState.CONFLICT:
        return f"{tag} = unsure ({_conflict_candidates(person)})"
    if state == IdentityState.KNOWN and name:
        return f"{tag} = {name} (known, {evidence})"
    if state == IdentityState.POSSIBLE and name:
        unseen = "" if Evidence.FACE in (person.get("evidence") or []) else ", face not seen yet"
        return f"{tag} = probably {name} (by {evidence}{unseen})"
    if state in (IdentityState.FAMILIAR, IdentityState.KNOWN, IdentityState.POSSIBLE):
        return f"{tag} = someone you have met before, no name on file (by {evidence})"
    return f"{tag} = unknown (tracked {_duration(float(person.get('tracked_sec') or 0.0))})"


def _conflict_candidates(person: PersonInViewDict) -> str:
    # runner_up_name is not in the published contract yet (see the report):
    # without it a conflict degrades to the one candidate the snapshot names.
    runner_up = _clean(dict(person).get("runner_up_name"))
    names = [name for name in (_clean(person.get("name")), runner_up) if name]
    if len(names) >= 2:
        return f"{names[0]} or {names[1]}"
    if names:
        return f"maybe {names[0]}, the evidence disagrees"
    return "the evidence disagrees"


def _evidence_text(person: PersonInViewDict) -> str:
    seen = person.get("evidence") or []
    words = [_EVIDENCE_WORDS[kind] for kind in Evidence if kind in seen]
    return ", ".join(words) if words else "no strong evidence"


def _box_text(person: PersonInViewDict) -> str:
    """The box in Gemini's per-mille [ymin, xmin, ymax, xmax], so a tag can be
    pointed at with go_to_point_in_view even when nothing is drawn."""
    box = person.get("bbox") or person.get("head_bbox")
    if not box or len(box) != 4:
        return ""
    return "[" + ", ".join(str(int(value)) for value in box) + "]"


def _facts_line(person: PersonInViewDict, now: float, context: set[str], allowance: int) -> str | None:
    """Ranked facts plus every open loop, trimmed from the tail to fit, nested
    under "If this is X" while the identity is only probable (RFC 5.3 rule 6)."""
    digest: PersonDigestDict = person.get("digest") or {}
    loops = [_loop_text(loop) for loop in (digest.get("open_loops") or []) if _clean(loop.get("text"))]
    facts = [text for text in (_clean(fact.get("text")) for fact in _rank_facts(digest, now, context)) if text]
    if not facts and not loops:
        return None
    name = _clean(person.get("name"))
    prefix = f"  If this is {name}: " if name and person.get("state") == IdentityState.POSSIBLE else "  Facts: "
    while facts and _tokens(prefix + "; ".join([*facts, *loops]) + ".") > allowance:
        facts.pop()
    return prefix + "; ".join([*facts, *loops]) + "."


def _loop_text(loop: OpenLoopDict) -> str:
    text = _clean(loop.get("text")) or ""
    due = _clean(loop.get("due"))
    return f"{text} (open, due {due})" if due else f"{text} (open)"


def _rank_facts(digest: PersonDigestDict, now: float, context: set[str]) -> list[FactDict]:
    """Importance x recency decay x lexical context match, best first."""
    facts = [fact for fact in (digest.get("facts") or []) if fact.get("kind") != _SENSITIVE]
    return sorted(facts, key=lambda fact: _fact_score(fact, now, context), reverse=True)[:_MAX_FACTS]


def _fact_score(fact: FactDict, now: float, context: set[str]) -> float:
    importance = float(fact.get("importance") or 0.5)
    age = max(0.0, now - float(fact.get("last_confirmed") or now))
    recency = _RECENCY_FLOOR + (1.0 - _RECENCY_FLOOR) * 0.5 ** (age / _RECENCY_HALF_LIFE_SEC)
    words = _words(fact.get("text") or "")
    overlap = len(words & context) / len(words) if words else 0.0
    return importance * recency * (1.0 + _CONTEXT_WEIGHT * overlap)


def _history_line(person: PersonInViewDict, now: float) -> str | None:
    digest: PersonDigestDict = person.get("digest") or {}
    last_seen: LastSeenDict = digest.get("last_seen") or {}
    parts: list[str] = []
    stamp = float(last_seen.get("stamp") or 0.0)
    if stamp and now - stamp >= _LAST_SEEN_QUIET_SEC:
        where = _clean(last_seen.get("map"))
        ago = _ago(now - stamp)
        parts.append(f"Last seen {ago} in {where}." if where else f"Last seen {ago}.")
    encounters = int(digest.get("encounters") or 0)
    if encounters > 1:
        parts.append(f"{encounters} encounters.")
    return "  " + " ".join(parts) if parts else None


def _episode_line(person: PersonInViewDict) -> str | None:
    digest: PersonDigestDict = person.get("digest") or {}
    closed = [ep for ep in (digest.get("episodes") or []) if ep.get("end") and _clean(ep.get("summary"))]
    return f"  Last time: {_clean(closed[-1].get('summary'))}" if closed else None


def _attention_lines(snapshot: PeopleSnapshotDict) -> list[str]:
    attention: AttentionDict = snapshot.get("attention") or {}
    text = _clean(attention.get("text"))
    return [f"Attention: {_sentence(text)}"] if text else []


def _recent_lines(snapshot: PeopleSnapshotDict, now: float, people: list[PersonInViewDict]) -> list[str]:
    """Who else was around today — one line, straight from the roster."""
    in_view = {person.get("person_id") for person in people}
    today = datetime.fromtimestamp(now).date()
    seen = [
        entry
        for entry in (snapshot.get("recent") or [])
        if _clean(entry.get("name"))
        and entry.get("person_id") not in in_view
        and datetime.fromtimestamp(float(entry.get("last_seen") or 0.0)).date() == today
    ]
    if not seen:
        return []
    newest = sorted(seen, key=lambda entry: float(entry.get("last_seen") or 0.0))[-_MAX_RECENT:]
    return ["Seen earlier today: " + ", ".join(_recent_text(entry) for entry in newest) + "."]


def _recent_text(entry: RecentPersonDict) -> str:
    clock = datetime.fromtimestamp(float(entry.get("last_seen") or 0.0)).strftime("%H:%M")
    where = _clean(entry.get("map"))
    return f"{_clean(entry.get('name'))} ({clock}, {where})" if where else f"{_clean(entry.get('name'))} ({clock})"


def _context_words(conversation: list[tuple[str, str]], events_text: list[str]) -> set[str]:
    """Content words of what is being said right now — the facts worth
    surfacing are the ones that overlap them."""
    words: set[str] = set()
    for _, text in conversation:
        words |= _words(text)
    for text in events_text:
        words |= _words(text)
    return words


def _words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9']+", text.lower()) if len(word) > 2 and word not in _STOP_WORDS}


def _tokens(text: str) -> int:
    return math.ceil(len(text.split()) * _TOKENS_PER_WORD)


def _sentence(text: str) -> str:
    """The node's own wording, terminated — the block is read as prose."""
    return text if text[-1] in ".!?…" else text + "."


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def _duration(seconds: float) -> str:
    return f"{round(seconds)} s" if seconds < 90 else f"{round(seconds / 60)} min"


def _ago(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{round(seconds / 60)} min ago"
    if seconds < 86400:
        hours = round(seconds / 3600)
        return f"{hours} hour ago" if hours == 1 else f"{hours} hours ago"
    days = int(seconds // 86400)
    return "yesterday" if days == 1 else f"{days} days ago"
