# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Where the head should point, read out of a ``/brain/people`` snapshot.

The people node already detected and chose everyone in view on this same camera
stream (docs/rfc/people-memory.md section 5.5), so the gaze loop takes its
answer instead of loading a second face model — the one argument the whole gaze
path rests on, stated here and referenced from ``gaze.py``. The answer is the
attention target's head box when there is one, otherwise the nearest live
person's, and the top of their body box while no face has been located yet.
PURE module: no rclpy, no cv2 — the tracker around it is the part that cannot
be unit-tested.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from brain_client.people.geometry import head_region

if TYPE_CHECKING:
    from brain_client.people.types import Box, PeopleSnapshotDict, PersonInViewDict

_FAR_AWAY = 1e6  # a person with no range yet sorts behind everyone who has one


def gaze_box(snapshot: PeopleSnapshotDict) -> Box | None:
    """The normalized head box worth looking at, or None when the snapshot has
    nobody live in it."""
    attended = _normalized((snapshot.get("attention") or {}).get("head_bbox"))
    if attended is not None:
        return attended
    person = _nearest(snapshot)
    if person is None:
        return None
    head = _normalized(person.get("head_bbox"))
    if head is not None:
        return head
    body = _normalized(person.get("bbox"))
    return head_region(body) if body is not None else None


def as_face(box: Box) -> dict[str, float]:
    """The box as the centre-and-size the gaze controller steers on."""
    ymin, xmin, ymax, xmax = box
    return {
        "center_x": (xmin + xmax) / 2.0,
        "center_y": (ymin + ymax) / 2.0,
        "width": xmax - xmin,
        "height": ymax - ymin,
    }


def _nearest(snapshot: PeopleSnapshotDict) -> PersonInViewDict | None:
    live = [person for person in (snapshot.get("people") or []) if not person.get("lost")]
    return min(live, key=_range_m) if live else None


def _range_m(person: PersonInViewDict) -> float:
    value = person.get("range_m")
    return float(value) if isinstance(value, (int, float)) else _FAR_AWAY


def _normalized(raw: object) -> Box | None:
    """A per-mille ``[ymin, xmin, ymax, xmax]`` off the wire as a unit box."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    ymin, xmin, ymax, xmax = (float(value) / 1000.0 for value in raw)
    return (ymin, xmin, ymax, xmax)
