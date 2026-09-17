# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

from dataclasses import dataclass

from lerobot.robots.config import RobotConfig

from .wire import DEFAULT_PORT_ACTIONS, DEFAULT_PORT_OBSERVATIONS


@RobotConfig.register_subclass("mars")
@dataclass
class MarsConfig(RobotConfig):
    # Hostname or IP of the robot running innate-os; `localhost` for the simulator or a lerobot
    # process on the Jetson itself.
    remote_ip: str = "mars.local"
    port_actions: int = DEFAULT_PORT_ACTIONS
    port_observations: int = DEFAULT_PORT_OBSERVATIONS
    connect_timeout_s: float = 5.0
    # How long get_observation() waits for a fresh frame before returning the previous one.
    poll_timeout_ms: int = 100
    # True while the Innate app or leader arm drives the robot: send_action() is recorded but
    # not forwarded, so the client never echoes a stale command behind the operator.
    external_commands: bool = False
