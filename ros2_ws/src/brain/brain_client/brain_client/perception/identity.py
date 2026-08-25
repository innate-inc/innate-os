# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The robot's self-knowledge for the agent's system prompt.

mars_app owns data/robot_info.json and republishes it on /robot/info at 1 Hz,
so a rename from the Settings page reaches the brain's next turn — no restart.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rclpy.node import Node
    from std_msgs.msg import String


@dataclass(frozen=True)
class RobotIdentity:
    name: str
    color: str | None = None
    hardware_revision: str | None = None
    version: str | None = None
    hostname: str | None = None
    wifi_ssid: str | None = None


def parse_identity(payload: str) -> RobotIdentity | None:
    try:
        info = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(info, dict):
        return None
    name = _clean(info, "robot_name")
    if name is None:
        return None
    return RobotIdentity(
        name=name,
        color=_clean(info, "color_variant"),
        hardware_revision=_clean(info, "hardware_revision"),
        version=_clean(info, "version"),
        hostname=_clean(info, "hostname"),
        wifi_ssid=_clean(info, "wifi_ssid"),
    )


# Fields reach the system prompt and any rosbridge client can write them —
# flattened and capped, a hostile value stays a phrase, not injected structure.
_MAX_FIELD_CHARS = 80


def _clean(info: dict, key: str) -> str | None:
    value = info.get(key)
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:_MAX_FIELD_CHARS] or None


class IdentityMonitor:
    def __init__(self, node: Node):
        # ROS import deferred so tests can construct this without rclpy.
        from std_msgs.msg import String

        self.current: RobotIdentity | None = None
        node.create_subscription(String, "/robot/info", self._on_info, 10)

    def _on_info(self, msg: String) -> None:
        identity = parse_identity(msg.data)
        if identity is not None:
            self.current = identity
