# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

from dataclasses import dataclass, field

from lerobot.teleoperators.config import TeleoperatorConfig

from .wire import DEFAULT_PORT_ACTIONS, DEFAULT_PORT_OBSERVATIONS, default_host


@TeleoperatorConfig.register_subclass("mars_passthrough")
@dataclass
class MarsPassthroughTeleopConfig(TeleoperatorConfig):
    """Reads back what the Innate app, leader arm, or a skill last commanded on the robot.

    Pair it with ``--robot.external_commands=true`` so the recorded action is the operator's
    command and the lerobot client never re-sends it.
    """

    remote_ip: str = field(default_factory=default_host)
    port_actions: int = DEFAULT_PORT_ACTIONS
    port_observations: int = DEFAULT_PORT_OBSERVATIONS
    connect_timeout_s: float = 5.0
    poll_timeout_ms: int = 5
