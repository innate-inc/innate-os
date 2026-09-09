# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The brain's window onto the people node.

Two subscriptions and one gate: ``/brain/people`` (latched, ≤ 5 Hz) is the
snapshot the turn's People block and overlay are rendered from, and
``/brain/people_events`` carries the handful of things worth waking a turn for
(a name learned, a known person back after ten minutes, a deep recall). Both
are always-on and cost nothing while the people node is absent — no publisher,
no callback — and the latched snapshot means a brain activated later starts
with the roster it left behind rather than an empty scene.

Recognition itself lives in the people node; nothing here decides anything.
The gate is only that: a snapshot of another schema, or one carrying numbers a
reader would have to guard against, never reaches the brain at all.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from typing import TYPE_CHECKING, cast

from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String

from brain_client.people.types import SNAPSHOT_SCHEMA

if TYPE_CHECKING:
    from collections.abc import Callable

    from rclpy.node import Node

    from brain_client.people.types import PeopleEventDict, PeopleSnapshotDict

SNAPSHOT_TOPIC = "/brain/people"
EVENTS_TOPIC = "/brain/people_events"
_NUMBERS = ("tracked_sec", "range_m", "bearing_deg", "lost_sec")

_SNAPSHOT_QOS = QoSProfile(
    depth=1,
    history=QoSHistoryPolicy.KEEP_LAST,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    reliability=QoSReliabilityPolicy.RELIABLE,
)
_EVENTS_QOS = QoSProfile(
    depth=10,
    history=QoSHistoryPolicy.KEEP_LAST,
    reliability=QoSReliabilityPolicy.RELIABLE,
)


class PeopleFeed:
    def __init__(self, node: Node):
        self._logger = node.get_logger()
        self._snapshot: PeopleSnapshotDict | None = None
        self._warned_schema = False
        # Set by the composition root: one call per people event, on the ROS
        # executor thread (the brain queues it like any other stimulus).
        self.on_event: Callable[[PeopleEventDict], None] | None = None
        node.create_subscription(String, SNAPSHOT_TOPIC, self._on_snapshot, _SNAPSHOT_QOS)
        node.create_subscription(String, EVENTS_TOPIC, self._on_event, _EVENTS_QOS)

    def latest(self) -> PeopleSnapshotDict | None:
        """The newest snapshot, however old — callers that care state a bound."""
        return self._snapshot

    def fresh(self, max_age_sec: float) -> PeopleSnapshotDict | None:
        """The newest snapshot if its own stamp is within ``max_age_sec``.

        Epoch seconds, not monotonic: the stamp is the people node's and the
        two processes share a clock.
        """
        snapshot = self._snapshot
        if snapshot is None or time.time() - float(snapshot.get("stamp") or 0.0) > max_age_sec:
            return None
        return snapshot

    def _on_snapshot(self, msg: String) -> None:
        """The one gate between the people node and the turn. The snapshot is
        latched TRANSIENT_LOCAL, so a payload the block or the overlay chokes
        on is replayed to every restart until the node publishes again — a
        single malformed message would end every turn until a reboot."""
        parsed = self._parse(msg.data, SNAPSHOT_TOPIC, require_schema=True)
        if parsed is None:
            return
        if not _numeric(parsed.get("stamp")):
            self._warn_once(f"[People] Ignoring a snapshot with no usable stamp on {SNAPSHOT_TOPIC}")
            return
        parsed["people"] = _drawable(parsed.get("people"))
        self._snapshot = cast("PeopleSnapshotDict", parsed)

    def _on_event(self, msg: String) -> None:
        # Events carry no schema of their own: they are read one field at a
        # time and nothing latches them.
        parsed = self._parse(msg.data, EVENTS_TOPIC, require_schema=False)
        if parsed is not None and self.on_event is not None:
            self.on_event(cast("PeopleEventDict", parsed))

    def _parse(self, payload: str, topic: str, *, require_schema: bool) -> dict | None:
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            self._logger.warn(f"[People] Ignoring malformed JSON on {topic}")
            return None
        if not isinstance(parsed, dict):
            return None
        schema = parsed.get("schema", None if require_schema else SNAPSHOT_SCHEMA)
        if schema == SNAPSHOT_SCHEMA:
            return parsed
        self._warn_once(f"[People] Ignoring schema {schema} on {topic}; this brain reads {SNAPSHOT_SCHEMA}")
        return None

    def _warn_once(self, message: str) -> None:
        """A publisher out of step is out of step at 5 Hz."""
        if self._warned_schema:
            return
        self._warned_schema = True
        self._logger.warn(message)


def _drawable(people: object) -> list[dict]:
    """The snapshot's people, minus any whose numbers the block and the overlay
    would have to parse: they read a box straight through ``int()`` and a stamp
    through ``float()``, and one entry that raises there costs the whole turn."""
    if not isinstance(people, list):
        return []
    return [person for person in people if isinstance(person, dict) and _measured(person)]


def _measured(person: dict) -> bool:
    if any(not _numeric(person[key]) for key in _NUMBERS if person.get(key) is not None):
        return False
    return all(_box(person[key]) for key in ("bbox", "head_bbox") if person.get(key) is not None)


def _box(value: object) -> bool:
    return isinstance(value, list) and len(value) == 4 and all(_numeric(edge) for edge in value)


def _numeric(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def decode_event_image(event: PeopleEventDict) -> bytes | None:
    """The event's crop (a new face, once) as JPEG bytes."""
    encoded = event.get("image_b64")
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded)
    except (binascii.Error, ValueError):
        return None
