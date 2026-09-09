# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing read of the people the robot's subconscious is tracking.

The people node does the seeing and the remembering; recognizing, naming and
recalling happen without anyone asking. A skill only reads the result, and —
when a person asks to be forgotten — says so. Declared like any interface::

    people: People

    def execute(self, who: str):
        person = self.people.find(who)
        if person is None:
            self.fail(f"I can't see {who} right now.")
        ...  # person.range_m, person.bearing_deg, refreshed every loop

``in_view`` and ``find`` answer from the latched ``/brain/people`` snapshot,
republished at up to 5 Hz while anyone is tracked, and go empty once that
snapshot goes stale — a latched message outlives the node that published it,
and a skill must never drive at a position no engine is confirming any more.
``forget`` is the one mutation the skill surface has: hand it the person
``find`` returned (or their tag) and it answers ``(ok, message)`` with a message
written to be said out loud, never blocking longer than a few seconds whether or
not a people node is listening. Naming is not on this surface — the name rules
of RFC 6.3 are the node's, and the app and the scribe are the two paths that
commit one.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import TYPE_CHECKING, Any

from brain_messages.srv import ForgetPerson
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String

from brain_client.people.sdk_parse import EMPTY_VIEW, PeopleView, PersonInView, RecentPerson, parse_snapshot
from brain_client.skills.types import cancellable_sleep

if TYPE_CHECKING:
    from rclpy.client import Client
    from rclpy.impl.rcutils_logger import RcutilsLogger
    from rclpy.node import Node

PEOPLE_TOPIC = "/brain/people"
FORGET_SERVICE = "/brain/people/forget"

_LATCHED_QOS = QoSProfile(
    depth=1,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    reliability=QoSReliabilityPolicy.RELIABLE,
)

# Every message on this surface is said out loud, so it names what the robot
# cannot do rather than the service that did not answer (which the log names).
_UNREACHABLE = "I can't reach the part of me that remembers people right now."
_SERVICE_WAIT_SEC = 2.0  # how long a mutation waits for the people node to exist
_CALL_TIMEOUT_SEC = 5.0  # and for it to answer once it does
_POLL_SEC = 0.05


class People:
    def __init__(self, node: Node, logger: RcutilsLogger):
        self._logger = logger
        self._lock = threading.Lock()
        self._latest = ""
        self._parsed_text = ""
        self._parsed = EMPTY_VIEW
        self._handed_out = ""
        # Own callback group: a skill thread parks on the service replies
        # below, and the node's default group is busy dispatching whatever
        # else the skills server is running.
        group = ReentrantCallbackGroup()
        node.create_subscription(String, PEOPLE_TOPIC, self._on_snapshot, _LATCHED_QOS, callback_group=group)
        self._forget_client = node.create_client(ForgetPerson, FORGET_SERVICE, callback_group=group)

    def in_view(self) -> list[PersonInView]:
        """Everyone the engine is tracking right now, nearest first."""
        view = self._read()
        return view.in_view() if view.is_fresh(time.time()) else []

    def find(self, name_or_tag: str) -> PersonInView | None:
        """The person in view that a tag ("P3"), a person id or a name means."""
        view = self._read()
        return view.find(name_or_tag) if view.is_fresh(time.time()) else None

    def recently_seen(self, minutes: float = 60.0) -> list[RecentPerson]:
        """Everyone seen in the last ``minutes``, most recent first. The node
        publishes the last day, so a longer window answers a day."""
        return self._view().recently_seen(minutes, time.time())

    def forget(self, who: PersonInView | str) -> tuple[bool, str]:
        """Delete everything known about a person — templates, outfits, facts,
        episodes, thumbnails — and tombstone their id. Irreversible.

        Hand back the :class:`PersonInView` ``find`` gave you whenever there is
        one: it names the snapshot the decision was read from, and a tag that
        belongs to somebody else by now is refused instead of deleting them."""
        request = ForgetPerson.Request()
        request.who = who.tag if isinstance(who, PersonInView) else str(who)
        # RFC section 8: a key of its own, so a reply lost on the way back costs
        # a repeat and not a second deletion.
        request.idempotency_key = str(uuid.uuid4())
        request.decided_on_stamp_ns = self._decided_on(who)
        return self._call(self._forget_client, request, FORGET_SERVICE)

    def _decided_on(self, who: PersonInView | str) -> str:
        """The snapshot this decision was read from, as the decimal nanoseconds
        the node compares against a track's first sighting. Never the newest
        snapshot: no tag can be newer than that, so quoting it back would turn
        RFC section 8's check off."""
        if isinstance(who, PersonInView):
            return str(int(who.stamp * 1e9)) if who.stamp > 0.0 else ""
        return self._handed_out

    def _read(self) -> PeopleView:
        """A view, remembered as the one a later ``forget(tag)`` read its tag
        from — the tag alone carries no snapshot with it."""
        view = self._view()
        self._handed_out = view.frame_stamp_ns
        return view

    def _view(self) -> PeopleView:
        with self._lock:
            if self._latest != self._parsed_text:
                self._parsed_text = self._latest
                self._parsed = parse_snapshot(self._latest)
            return self._parsed

    def _on_snapshot(self, msg: String) -> None:
        with self._lock:
            self._latest = msg.data

    def _call(self, client: Client, request: Any, service: str) -> tuple[bool, str]:
        """One mutation, bounded at both ends. Every wait is sliced through
        cancellable_sleep so a Stop unwinds the skill instead of serving out
        the timeout (it raises SkillCancelled)."""
        deadline = time.monotonic() + _SERVICE_WAIT_SEC
        while not client.service_is_ready():
            if time.monotonic() >= deadline:
                self._logger.warn(f"{service} is not available")
                return False, _UNREACHABLE
            cancellable_sleep(_POLL_SEC)

        future = client.call_async(request)
        deadline = time.monotonic() + _CALL_TIMEOUT_SEC
        while not future.done():
            if time.monotonic() >= deadline:
                future.cancel()
                self._logger.warn(f"{service} did not answer within {_CALL_TIMEOUT_SEC:.0f} s")
                return False, _UNREACHABLE
            cancellable_sleep(_POLL_SEC)

        response = future.result()
        if response is None:
            self._logger.warn(f"{service} returned no result")
            return False, _UNREACHABLE
        return bool(response.success), str(response.message)
