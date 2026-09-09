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
``forget`` is the one mutation the skill surface has: it returns
``(ok, message)`` with a message written to be said out loud, and never blocks
longer than a few seconds whether or not a people node is listening. Naming is
not on this surface — the name rules of RFC 6.3 are the node's, and the app and
the scribe are the two paths that commit one.
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
        # Own callback group: a skill thread parks on the service replies
        # below, and the node's default group is busy dispatching whatever
        # else the skills server is running.
        group = ReentrantCallbackGroup()
        node.create_subscription(String, PEOPLE_TOPIC, self._on_snapshot, _LATCHED_QOS, callback_group=group)
        self._forget_client = node.create_client(ForgetPerson, FORGET_SERVICE, callback_group=group)

    def in_view(self) -> list[PersonInView]:
        """Everyone the engine is tracking right now, nearest first."""
        view = self._view()
        return view.in_view() if view.is_fresh(time.time()) else []

    def find(self, name_or_tag: str) -> PersonInView | None:
        """The person in view that a tag ("P3"), a person id or a name means."""
        view = self._view()
        return view.find(name_or_tag) if view.is_fresh(time.time()) else None

    def recently_seen(self, minutes: float = 60.0) -> list[RecentPerson]:
        """Everyone seen in the last ``minutes``, most recent first."""
        return self._view().recently_seen(minutes, time.time())

    def forget(self, who: str) -> tuple[bool, str]:
        """Delete everything known about a person — templates, outfits, facts,
        episodes, thumbnails — and tombstone their id. Irreversible."""
        request = ForgetPerson.Request()
        request.who = str(who)
        self._decide(request)
        return self._call(self._forget_client, request, FORGET_SERVICE)

    def _decide(self, request: Any) -> None:
        """RFC section 8: a mutation names the snapshot it was decided on, so a
        tag issued after it fails instead of landing on a stranger, and carries
        a key of its own, so a reply lost on the way back costs a repeat and not
        a second deletion."""
        request.idempotency_key = str(uuid.uuid4())
        request.decided_on_stamp_ns = self._view().frame_stamp_ns

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
                return False, f"{service} is not available — is the people node running?"
            cancellable_sleep(_POLL_SEC)

        future = client.call_async(request)
        deadline = time.monotonic() + _CALL_TIMEOUT_SEC
        while not future.done():
            if time.monotonic() >= deadline:
                future.cancel()
                return False, f"{service} did not answer within {_CALL_TIMEOUT_SEC:.0f} s"
            cancellable_sleep(_POLL_SEC)

        response = future.result()
        if response is None:
            return False, f"{service} returned no result"
        return bool(response.success), str(response.message)
