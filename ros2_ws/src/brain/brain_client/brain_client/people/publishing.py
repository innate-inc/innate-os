# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""What the node is allowed to say about the people it saw: which tracks a
snapshot may still claim, the conflicts and the seek hint stamped onto them,
whether anything moved enough to be worth republishing, and the payloads an
idle node latches and a ``GetPeople`` call answers with (RFC 5.3-5.5). PURE
module: no rclpy, no I/O."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from brain_client.people.types import SNAPSHOT_SCHEMA, IdentityState

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from brain_client.people.resolve import Resolution
    from brain_client.people.types import (
        AttentionDict,
        HealthDict,
        PeopleRosterDict,
        PeopleSnapshotDict,
        RosterEntryDict,
        TrackState,
    )

SNAPSHOT_HEARTBEAT_SEC = 5.0
"""The brain reads a snapshot older than 10 s as a claim about nobody; half
that is the heartbeat that keeps an idle node's health readable."""

LOST_GRACE_SEC = 60.0  # a lost track rides the snapshot this long, as "just left view"
SEEK_MIN_RANGE_M = 1.5  # closer than this, walking over gains nothing the head tilt cannot


def publishable_tracks(
    tracks: Sequence[TrackState], now: float, *, grace_sec: float = LOST_GRACE_SEC
) -> list[TrackState]:
    """Live tracks, plus the ones lost recently enough to still be worth saying
    "just left view" about; the tracker remembers lost tracks for five minutes,
    far longer than the brain wants to hear about them."""
    return [track for track in tracks if not track.lost or now - track.last_seen <= grace_sec]


def fresh_tracks(
    tracks: Sequence[TrackState], *, now: float, last_frame_at: float, stale_sec: float
) -> tuple[TrackState, ...]:
    """The tracks a silent camera is still allowed to claim: none. The tracker
    ages a track out on the tick that misses it, so without frames the scene
    would freeze with everybody still in it (RFC section 9)."""
    if now - last_frame_at > stale_sec:
        return ()
    return tuple(tracks)


def apply_conflicts(tracks: Sequence[TrackState], resolutions: Mapping[str, Resolution]) -> list[TrackState]:
    """One person cannot be two live tracks (RFC 5.3.4). The resolver reports
    the clash and keeps the loser's belief; the snapshot has to *say* it, so
    the track reads ``conflict``, person and name kept — the rival identity is
    this person's own other track, never a second candidate."""
    resolved: list[TrackState] = []
    for track in tracks:
        resolution = resolutions.get(track.tag)
        if resolution is None or resolution.conflict_with is None:
            resolved.append(track)
            continue
        resolved.append(replace(track, identity=replace(track.identity, state=IdentityState.CONFLICT)))
    return resolved


def seek_hint(
    attention: AttentionDict | None, tracks: Sequence[TrackState], *, seek_faces: bool
) -> AttentionDict | None:
    """With ``seek_faces`` on, the attention line names the skill that would get
    the face (RFC 5.5: approach is opt-in, and it stays a nudge in the block —
    the node never calls a tool)."""
    if attention is None or not seek_faces:
        return attention
    tag = attention.get("tag") or ""
    target = next((track for track in tracks if track.tag == tag), None)
    if target is None or target.frames_with_face > 0:
        return attention
    if target.range_m is not None and target.range_m <= SEEK_MIN_RANGE_M:
        return attention
    return {**attention, "text": f"{attention.get('text', '')}; approach_person({tag}) would get a look"}


def snapshot_changed(current: PeopleSnapshotDict, previous: PeopleSnapshotDict | None) -> bool:
    """Whether anything but the clock moved: an idle scene must not republish a
    kilobyte of identical JSON five times a second."""
    if previous is None:
        return True
    volatile = ("stamp", "frame_stamp_ns")
    return {key: value for key, value in current.items() if key not in volatile} != {
        key: value for key, value in previous.items() if key not in volatile
    }


def should_publish(
    current: PeopleSnapshotDict,
    previous: PeopleSnapshotDict | None,
    *,
    now: float,
    last_publish: float,
    active: bool,
    heartbeat_sec: float = SNAPSHOT_HEARTBEAT_SEC,
) -> bool:
    """Every tick while anyone is in view, on any change otherwise, and at least
    every ``heartbeat_sec`` so health stays readable."""
    return active or now - last_publish >= heartbeat_sec or snapshot_changed(current, previous)


def roster_answer(
    snapshot: PeopleSnapshotDict, *, roster: Sequence[RosterEntryDict] | None, capacity_full: bool
) -> PeopleRosterDict:
    """``GetPeople``'s payload: the live snapshot, plus the roster when it was
    asked for. A missing ``roster`` key reads as "this node is too old" in the
    Settings page, so it is present exactly when it was requested."""
    answer = cast("PeopleRosterDict", dict(snapshot))
    if roster is None:
        return answer
    answer["roster"] = list(roster)
    answer["capacity_full"] = capacity_full
    return answer


def empty_snapshot(health: HealthDict, now: float, *, collection_enabled: bool) -> PeopleSnapshotDict:
    """What the node latches before its first tick, and while it is idle — an
    honest "nobody, and here is the state of the sensors" rather than silence."""
    return {
        "schema": SNAPSHOT_SCHEMA,
        "stamp": round(now, 3),
        "frame_stamp_ns": None,
        "image_size": [640, 480],
        "health": health,
        "collection_enabled": collection_enabled,
        "attention": None,
        "people": [],
        "recent": [],
    }
