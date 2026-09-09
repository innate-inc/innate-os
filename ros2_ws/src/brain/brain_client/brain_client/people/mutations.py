# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The bookkeeping the five services do before they touch the store (RFC
section 8): the answers a retry is replayed from, the check that refuses a tag
minted after the snapshot the caller decided on, and ``who`` — a live tag, a
person id or a name — resolved to one person. PURE module: no rclpy, no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from brain_client.people.types import TAG_RE

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from brain_client.people.types import RosterEntryDict, TrackState

IDEMPOTENCY_KEYS = 64
"""How many mutation keys the node answers a retry from. A key is spent within
seconds of being minted, so this is a few minutes of the busiest Settings page."""
DECISION_SKEW_SEC = 1.0
"""How far a track may have started past the frame stamp it was measured on and
still belong to that snapshot: the stamp is the capture, and the tracker's clock
starts when the tick that decoded the frame ran, a camera latency later."""
STALE_DECISION_MESSAGE = "that tag was issued after the snapshot you decided on; look again"


@dataclass(frozen=True)
class MutationResult:
    """What a mutation answered, kept so a retry can be answered the same way.
    ``person_id`` is empty for the services whose response has no such field."""

    success: bool
    message: str
    person_id: str = ""


class MutationLog:
    """The answers the last :data:`IDEMPOTENCY_KEYS` idempotency keys got.

    A call carrying a key this node has already answered is a retry — a reply
    the caller never received, a second tap on the Settings page — and RFC
    section 8 answers it from here rather than merging two people twice. An
    empty key is no key: those calls are always fresh mutations.
    """

    def __init__(self, limit: int = IDEMPOTENCY_KEYS) -> None:
        self._limit = limit
        self._answers: dict[str, MutationResult] = {}

    def answered(self, key: str) -> MutationResult | None:
        return self._answers.get(key.strip()) if key.strip() else None

    def remember(self, key: str, result: MutationResult) -> None:
        key = key.strip()
        if not key:
            return
        self._answers.pop(key, None)  # re-inserted last, so a live key is not the one evicted
        self._answers[key] = result
        while len(self._answers) > self._limit:
            del self._answers[next(iter(self._answers))]


def tag_newer_than_decision(who: str, tracks: Sequence[TrackState], decided_on_stamp_ns: str) -> bool:
    """Whether ``who`` names a live tag whose track started after the snapshot
    the caller decided on (RFC section 8). A caller acting on a tag its snapshot
    never carried is answering about somebody it has not seen, so the mutation
    is refused instead of landing on whoever holds that tag now."""
    decided_on = _stamp_seconds(decided_on_stamp_ns)
    query = who.strip()
    if decided_on is None or not TAG_RE.match(query):
        return False
    track = next((t for t in tracks if t.tag.upper() == query.upper()), None)
    return track is not None and track.first_seen > decided_on + DECISION_SKEW_SEC


def _stamp_seconds(stamp_ns: str) -> float | None:
    """``frame_stamp_ns`` as epoch seconds; None when there is no stamp to check."""
    try:
        return int(stamp_ns) / 1e9
    except (TypeError, ValueError):
        return None


def resolve_who(
    who: str,
    tracks: Sequence[TrackState],
    roster: Sequence[RosterEntryDict],
    *,
    forgotten: Callable[[str], bool] = lambda _person_id: False,
) -> tuple[str | None, str]:
    """``who`` — a live tag, a person id, or a name — as a person id, or None
    with a message the caller can say out loud. A tag that has expired, or a
    name two people answer to, is an error: never the nearest live track."""
    query = who.strip()
    if not query:
        return None, "no person given: pass a live tag like P3, or a person id"
    if TAG_RE.match(query):
        track = next((t for t in tracks if t.tag.upper() == query.upper()), None)
        if track is None:
            return None, f"{query} is not a track I have any more — try again while they are in view"
        if track.identity.person_id is None:
            return None, f"{query} is not somebody I have on file yet"
        return track.identity.person_id, ""
    if any(entry.get("person_id") == query for entry in roster):
        return query, ""
    if query.startswith("person_"):
        return None, f"I have nobody with the id {query}" + (" — that one was forgotten" if forgotten(query) else "")
    matches = [entry for entry in roster if (entry.get("name") or "").strip().casefold() == query.casefold()]
    if len(matches) == 1:
        return matches[0].get("person_id"), ""
    if matches:
        return None, f"I know {len(matches)} people called {query} — say which one by their id"
    return None, f"I do not know anybody called {query}"
