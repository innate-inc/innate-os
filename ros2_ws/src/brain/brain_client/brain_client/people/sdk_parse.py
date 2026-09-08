# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""What a skill reads off the ``/brain/people`` snapshot, and the parse of it.

PURE module: the ROS side — the latched subscription and the two services —
is :mod:`brain_client.robot.people`, and everything a skill's answer actually
depends on lives here, as a function of the JSON text.

Tolerant by construction, because the node and the skills ship separately: a
field that is missing, null or the wrong type costs that one field, never the
snapshot. Boxes arrive as Gemini's per-mille ints and are handed on as the
normalized :data:`~brain_client.people.types.Box` the rest of the tree reasons
in; stamps stay epoch seconds.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from brain_client.people.types import Box, IdentityState

SNAPSHOT_FRESH_SEC = 3.0
"""How old a snapshot may be and still say who is in front of the robot: the
node republishes at up to 5 Hz while any track is live, and a latched message
outlives the node that published it."""


@dataclass(frozen=True)
class PersonInView:
    """One person the engine is tracking right now. ``bbox``/``head_bbox`` are
    normalized ``(ymin, xmin, ymax, xmax)`` of the published 640x480 frame,
    ``bearing_deg`` is positive to the robot's left, and ``state`` is what the
    accumulated evidence supports — only ``known`` carries a confirmed name."""

    tag: str
    person_id: str | None = None
    name: str | None = None
    state: IdentityState = IdentityState.UNKNOWN
    bbox: Box | None = None
    head_bbox: Box | None = None
    range_m: float | None = None
    bearing_deg: float | None = None
    tracked_sec: float = 0.0
    description: str | None = None


@dataclass(frozen=True)
class RecentPerson:
    """Somebody seen earlier and not in view now."""

    person_id: str
    name: str | None = None
    last_seen: float = 0.0
    map_name: str | None = None


@dataclass(frozen=True)
class PeopleView:
    """One parsed snapshot: who is in view, and who was seen before them.

    ``frame_stamp_ns`` names the frame the boxes were measured on, as the
    decimal string the node published; a mutation quotes it back as the
    snapshot it was decided on (RFC section 8).
    """

    stamp: float = 0.0
    people: tuple[PersonInView, ...] = ()
    recent: tuple[RecentPerson, ...] = ()
    frame_stamp_ns: str = ""

    def is_fresh(self, now: float) -> bool:
        return self.stamp > 0.0 and now - self.stamp <= SNAPSHOT_FRESH_SEC

    def in_view(self) -> list[PersonInView]:
        """Everyone in view, nearest first; people with no range estimate last."""
        return sorted(self.people, key=lambda person: person.range_m if person.range_m is not None else math.inf)

    def find(self, name_or_tag: str) -> PersonInView | None:
        """The person a tag, a person id or a name (case-insensitive) means.
        Names are not unique — with two Anas in view this is the nearer one."""
        query = name_or_tag.strip()
        if not query:
            return None
        people = self.in_view()
        exact = next((person for person in people if query in (person.tag, person.person_id)), None)
        if exact is not None:
            return exact
        lowered = query.casefold()
        return next((p for p in people if p.name is not None and p.name.casefold() == lowered), None)

    def recently_seen(self, minutes: float, now: float) -> list[RecentPerson]:
        """Everyone seen within the window, most recent first: the node's
        digest of who was around, plus whoever is in view right now — a person
        standing in front of the robot was seen zero seconds ago."""
        since = now - minutes * 60.0
        seen: dict[str, RecentPerson] = {}
        for person in self.people:
            if person.person_id is not None:
                seen[person.person_id] = RecentPerson(person.person_id, person.name, self.stamp)
        for entry in self.recent:
            seen.setdefault(entry.person_id, entry)
        return sorted(
            (entry for entry in seen.values() if entry.last_seen >= since),
            key=lambda entry: entry.last_seen,
            reverse=True,
        )


EMPTY_VIEW = PeopleView()
"""What every consumer sees before the first snapshot arrives."""


def parse_snapshot(json_text: str) -> PeopleView:
    """The latest ``/brain/people`` message as a view; an empty view for
    anything that is not a snapshot object."""
    try:
        payload = json.loads(json_text) if json_text else None
    except (json.JSONDecodeError, TypeError):
        return EMPTY_VIEW
    if not isinstance(payload, dict):
        return EMPTY_VIEW
    return PeopleView(
        stamp=_as_float(payload.get("stamp")) or 0.0,
        people=_people(payload.get("people")),
        recent=_recent(payload.get("recent")),
        frame_stamp_ns=_as_str(payload.get("frame_stamp_ns")) or "",
    )


def _people(raw: Any) -> tuple[PersonInView, ...]:
    if not isinstance(raw, list):
        return ()
    parsed = (_person(entry) for entry in raw if isinstance(entry, dict))
    return tuple(person for person in parsed if person is not None)


def _person(entry: dict[str, Any]) -> PersonInView | None:
    tag = _as_str(entry.get("tag"))
    # A lost track rides the snapshot for a minute so the brain can still talk
    # about the person; nobody is in view any more, so the SDK drops it.
    if tag is None or entry.get("lost") is True:
        return None
    return PersonInView(
        tag=tag,
        person_id=_as_str(entry.get("person_id")),
        name=_as_str(entry.get("name")),
        state=_as_state(entry.get("state")),
        bbox=_as_box(entry.get("bbox")),
        head_bbox=_as_box(entry.get("head_bbox")),
        range_m=_as_float(entry.get("range_m")),
        bearing_deg=_as_float(entry.get("bearing_deg")),
        tracked_sec=_as_float(entry.get("tracked_sec")) or 0.0,
        description=_as_str(entry.get("description")),
    )


def _recent(raw: Any) -> tuple[RecentPerson, ...]:
    if not isinstance(raw, list):
        return ()
    entries = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        person_id = _as_str(item.get("person_id"))
        if person_id is None:
            continue
        entries.append(
            RecentPerson(
                person_id=person_id,
                name=_as_str(item.get("name")),
                last_seen=_as_float(item.get("last_seen")) or 0.0,
                map_name=_as_str(item.get("map")),
            )
        )
    return tuple(entries)


def _as_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _as_state(value: Any) -> IdentityState:
    # A state this SDK has never heard of is not an identity claim it can act
    # on, so it reads as "unknown" instead of failing the whole entry.
    try:
        return IdentityState(value)
    except ValueError:
        return IdentityState.UNKNOWN


def _as_box(value: Any) -> Box | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    edges: list[float] = []
    for edge in value:
        number = _as_float(edge)
        if number is None:
            return None
        edges.append(number / 1000.0)
    return (edges[0], edges[1], edges[2], edges[3])
