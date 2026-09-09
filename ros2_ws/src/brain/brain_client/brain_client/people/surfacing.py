# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Surfacing: what the subconscious tells the rest of the robot about people
(docs/rfc/people-memory.md sections 5.5 and 6.4 in innate-jetson).

Three jobs, all pure. :func:`build_snapshot` turns the engine's tracks plus the
store into the ``/brain/people`` payload — boxes in Gemini's per-mille
convention, a memory digest for everyone the robot has a claim on, and the
people it saw over the last day. :func:`choose_attention` decides whose face is
worth chasing next and says so in one line the agent can act on in
conversation. :class:`PeopleEvents` turns changes into the bounded, per-person
cooled-down wake events on ``/brain/people_events``.

Nothing here decides identity or writes memory; it reads what the engine and
the scribe already decided. PURE module: no rclpy, no cv2, no network.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from brain_client.people.types import SETTLED_STATES, SNAPSHOT_SCHEMA, EventKind, IdentityState

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from brain_client.people.store import PeopleStore
    from brain_client.people.types import (
        AttentionDict,
        Box,
        HealthDict,
        PeopleEventDict,
        PeopleSnapshotDict,
        PersonInViewDict,
        RecentPersonDict,
        TrackState,
    )

DIGEST_STATES = (IdentityState.FAMILIAR, IdentityState.POSSIBLE, IdentityState.KNOWN)
"""Who gets a memory digest: everyone the robot has a claim on, tentative or not."""

RECENT_WINDOW_SEC = 86400.0
"""How far back the ``recent`` digest reaches. A day, not local midnight: the
skill SDK's ``recently_seen(minutes)`` reads this list, and a window that ended
at midnight silently answered "nobody in the last hour" at 00:30."""

ATTENTION_NEAR_M = 2.0  # "any unresolved person within 2 m" (RFC 5.5)
FACE_STALE_SEC = 5.0  # a face seen longer ago than this reads as "turned aside"
REENTRY_GAP_SEC = 600.0
_COOLDOWN_SEC: dict[EventKind, float] = {
    EventKind.ENROLLED: 3600.0,
    EventKind.REENTERED: REENTRY_GAP_SEC,
    EventKind.NAME_LEARNED: 3600.0,
    EventKind.DISAMBIGUATION: 120.0,
    EventKind.CONFLICT: 300.0,
    EventKind.RECALLED: 30.0,
}


def per_mille(box: Box) -> list[int]:
    """A normalized box as Gemini's ``[ymin, xmin, ymax, xmax]`` per-mille ints
    — the convention the overlay, the webapp and ``go_to_point_in_view`` share."""
    return [min(1000, max(0, round(value * 1000))) for value in box]


def build_snapshot(
    tracks: Sequence[TrackState],
    store: PeopleStore,
    health: HealthDict,
    now: float,
    *,
    frame_stamp_ns: str | None = None,
    image_size: tuple[int, int] = (640, 480),
    attention: AttentionDict | None = None,
    learned: Mapping[str, str] | None = None,
    hints: Mapping[str, str] | None = None,
) -> PeopleSnapshotDict:
    """The ``/brain/people`` payload for one tick.

    ``learned`` and ``hints`` are the scribe's per-tag lines (a name just
    committed, a disambiguation question worth asking); both ride the person
    they are about and are the node's to clear once shown.
    """
    people = [
        _person(track, store, now, (learned or {}).get(track.tag), (hints or {}).get(track.tag)) for track in tracks
    ]
    in_view = {track.identity.person_id for track in tracks if track.identity.person_id}
    recent: list[RecentPersonDict] = [
        entry for entry in store.recent(now - RECENT_WINDOW_SEC) if entry.get("person_id") not in in_view
    ]
    return {
        "schema": SNAPSHOT_SCHEMA,
        "stamp": round(now, 3),
        "frame_stamp_ns": frame_stamp_ns,
        "image_size": [image_size[0], image_size[1]],
        "health": health,
        "collection_enabled": store.collection_enabled(),
        "attention": attention,
        "people": people,
        "recent": recent,
    }


def _person(
    track: TrackState, store: PeopleStore, now: float, learned: str | None, hint: str | None
) -> PersonInViewDict:
    identity = track.identity
    person_id = identity.person_id
    digest = store.digest(person_id, now) if person_id is not None and identity.state in DIGEST_STATES else None
    return {
        "tag": track.tag,
        "person_id": person_id,
        "name": identity.name,
        "state": str(identity.state),
        "evidence": [str(evidence) for evidence in identity.evidence],
        "confidence": round(identity.confidence, 3),
        "bbox": per_mille(track.box),
        "head_bbox": per_mille(track.head_box) if track.head_box is not None else None,
        "range_m": round(track.range_m, 2) if track.range_m is not None else None,
        "bearing_deg": round(track.bearing_deg, 1) if track.bearing_deg is not None else None,
        "tracked_sec": round(max(0.0, now - track.first_seen), 1),
        "lost": track.lost,
        # A lost track rides the snapshot for a minute; without its age every
        # reader has to say "just left view" for the whole minute.
        "lost_sec": round(max(0.0, now - track.last_seen), 1) if track.lost else None,
        "description": store.description(person_id) if person_id is not None else None,
        "hint": hint,
        "learned": learned,
        "digest": digest,
    }


# ------------------------------------------------------------------ attention


def choose_attention(tracks: Sequence[TrackState], now: float) -> AttentionDict | None:
    """Whose face the engine should chase, per RFC 5.5: someone talking to the
    robot it has not settled, then anyone unresolved within 2 m, then anyone
    unresolved at all. None when everybody in view is settled — a face the
    engine already confirmed is not worth a second look, named or not."""
    live = [track for track in tracks if not track.lost and track.identity.state not in SETTLED_STATES]
    if not live:
        return None
    speaking = [track for track in live if track.speaking]
    near = [track for track in live if track.range_m is not None and track.range_m <= ATTENTION_NEAR_M]
    target = _closest(speaking) or _closest(near) or _closest(live)
    if target is None:
        return None
    return {
        "tag": target.tag,
        "text": _attention_text(target, now),
        "head_bbox": per_mille(target.head_box) if target.head_box is not None else None,
    }


def _closest(tracks: Sequence[TrackState]) -> TrackState | None:
    """Nearest first; a track with no range yet sorts last but still counts."""
    if not tracks:
        return None
    return min(tracks, key=lambda track: (track.range_m if track.range_m is not None else 1e6, track.first_seen))


def _attention_text(track: TrackState, now: float) -> str:
    where = f"{track.range_m:.1f} m away" if track.range_m is not None else "distance unknown"
    if track.frames_with_face == 0 or track.last_face_stamp is None:
        why = "face not seen yet"
    elif now - track.last_face_stamp > FACE_STALE_SEC:
        why = "turned aside"
    else:
        why = "still deciding"
    return f"trying to see {track.tag}'s face ({where}, {why})"


# --------------------------------------------------------------------- events


class PeopleEvents:
    """The bounded wake events, one cooldown per person per kind so a person
    walking in and out of frame cannot flood the brain."""

    def __init__(self, *, reentry_gap_sec: float = REENTRY_GAP_SEC, cooldowns: Mapping[EventKind, float] | None = None):
        self._reentry_gap_sec = reentry_gap_sec
        self._cooldowns = dict(_COOLDOWN_SEC) | dict(cooldowns or {})
        self._last: dict[tuple[EventKind, str], float] = {}
        self._seen: dict[str, float] = {}  # person id -> when this process last had them in view

    def emit(
        self,
        tracks: Sequence[TrackState],
        now: float,
        *,
        enrolled: Mapping[str, bytes | None] | None = None,
        learned: Mapping[str, str] | None = None,
        hints: Mapping[str, str] | None = None,
    ) -> list[PeopleEventDict]:
        """The events this tick earns. ``enrolled`` maps the tag of a person the
        engine just created to the crop worth showing once; ``learned`` and
        ``hints`` are the scribe's per-tag lines."""
        self._prune(now)
        events: list[PeopleEventDict] = []
        for track in tracks:
            if track.lost:
                continue
            events.extend(self._for_track(track, now, enrolled or {}, learned or {}, hints or {}))
        return events

    def _prune(self, now: float) -> None:
        """Half these keys are tags, which are never reused, so an entry older
        than the longest cooldown can never suppress anything again."""
        horizon = max(self._cooldowns.values(), default=0.0)
        self._last = {key: last for key, last in self._last.items() if now - last <= horizon}

    def recalled(self, person_id: str, name: str | None, text: str, now: float) -> PeopleEventDict | None:
        """A deep recall that came back with something (RFC 6.4)."""
        if not text.strip():
            return None
        return self._event(
            EventKind.RECALLED,
            person_id or "recall",
            now,
            tag=None,
            person_id=person_id or None,
            name=name,
            text=f"Recalled about {name or 'them'}: {text.strip()}",
        )

    def _for_track(
        self,
        track: TrackState,
        now: float,
        enrolled: Mapping[str, bytes | None],
        learned: Mapping[str, str],
        hints: Mapping[str, str],
    ) -> list[PeopleEventDict]:
        identity = track.identity
        person_id = identity.person_id
        label = identity.name or track.tag
        events: list[PeopleEventDict] = []
        if track.tag in enrolled:
            crop = enrolled[track.tag]
            event = self._event(
                EventKind.ENROLLED,
                person_id or track.tag,
                now,
                tag=track.tag,
                person_id=person_id,
                name=identity.name,
                text=f"{track.tag} is someone new — remembering this face",
                image_b64=base64.b64encode(crop).decode() if crop else None,
            )
            if event is not None:
                events.append(event)
        elif person_id is not None and identity.state in DIGEST_STATES:
            gap = now - self._seen[person_id] if person_id in self._seen else None
            if gap is None or gap >= self._reentry_gap_sec:
                event = self._event(
                    EventKind.REENTERED,
                    person_id,
                    now,
                    tag=track.tag,
                    person_id=person_id,
                    name=identity.name,
                    text=f"{label} is here" + (f" again, {round(gap / 60)} min since last seen" if gap else ""),
                )
                if event is not None:
                    events.append(event)
        if person_id is not None:
            self._seen[person_id] = now
        line = learned.get(track.tag)
        if line:
            event = self._event(
                EventKind.NAME_LEARNED,
                person_id or track.tag,
                now,
                tag=track.tag,
                person_id=person_id,
                name=identity.name,
                text=line,
            )
            if event is not None:
                events.append(event)
        hint = hints.get(track.tag)
        if hint:
            event = self._event(
                EventKind.DISAMBIGUATION,
                person_id or track.tag,
                now,
                tag=track.tag,
                person_id=person_id,
                name=identity.name,
                text=hint,
            )
            if event is not None:
                events.append(event)
        if identity.state is IdentityState.CONFLICT:
            event = self._event(
                EventKind.CONFLICT,
                person_id or track.tag,
                now,
                tag=track.tag,
                person_id=person_id,
                name=identity.name,
                text=f"not sure who {track.tag} is — the evidence disagrees, so learning is frozen",
            )
            if event is not None:
                events.append(event)
        return events

    def _event(
        self,
        kind: EventKind,
        key: str,
        now: float,
        *,
        tag: str | None,
        person_id: str | None,
        name: str | None,
        text: str,
        image_b64: str | None = None,
    ) -> PeopleEventDict | None:
        last = self._last.get((kind, key))
        if last is not None and now - last < self._cooldowns.get(kind, 0.0):
            return None
        self._last[(kind, key)] = now
        return {
            "kind": str(kind),
            "stamp": round(now, 3),
            "tag": tag,
            "person_id": person_id,
            "name": name,
            "text": text,
            "image_b64": image_b64,
        }
