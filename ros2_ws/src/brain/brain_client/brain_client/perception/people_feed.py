# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The brain's window onto the people node: one latched subscription.

Costs nothing while that node is absent — no publisher, no callback — and the
latched snapshot means a brain activated later starts with what the node
already published. Nothing here decides anything; the gate is only that a
snapshot of another schema, or one carrying a shape the block would have to
guard against, never reaches the turn at all.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String

if TYPE_CHECKING:
    from rclpy.node import Node

SNAPSHOT_TOPIC = "/brain/people"
SNAPSHOT_SCHEMA = 1

_QOS = QoSProfile(
    depth=1,
    history=QoSHistoryPolicy.KEEP_LAST,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    reliability=QoSReliabilityPolicy.RELIABLE,
)


class PeopleFeed:
    def __init__(self, node: Node) -> None:
        self._logger = node.get_logger()
        self._snapshot: dict | None = None
        self._warned = False
        node.create_subscription(String, SNAPSHOT_TOPIC, self._on_snapshot, _QOS)

    def fresh(self, max_age_sec: float) -> dict | None:
        """The newest snapshot if the people in it were seen within
        ``max_age_sec`` — its ``observed`` stamp, not the heartbeat's, so a
        camera outage ages the room out instead of restamping it. Epoch
        seconds: the two processes share a clock."""
        snapshot = self._snapshot
        if snapshot is None or time.time() - float(snapshot["observed"]) > max_age_sec:
            return None
        return snapshot

    def _on_snapshot(self, msg: String) -> None:
        """The one gate between the people node and the turn. The topic is
        latched, so a payload the block chokes on would be replayed to every
        restart until the node published again."""
        try:
            parsed = json.loads(msg.data)
        except (json.JSONDecodeError, TypeError):
            self._warn("[People] Ignoring malformed JSON on /brain/people")
            return
        if not isinstance(parsed, dict) or not all(
            isinstance(parsed.get(key), (int, float)) for key in ("stamp", "observed")
        ):
            return
        if parsed.get("schema") != SNAPSHOT_SCHEMA:
            self._warn(f"[People] Ignoring schema {parsed.get('schema')}; this brain reads {SNAPSHOT_SCHEMA}")
            return
        people = parsed.get("people")
        parsed["people"] = [person for person in people if isinstance(person, dict)] if isinstance(people, list) else []
        self._snapshot = parsed

    def _warn(self, message: str) -> None:
        """A publisher out of step is out of step at every tick."""
        if not self._warned:
            self._warned = True
            self._logger.warn(message)
