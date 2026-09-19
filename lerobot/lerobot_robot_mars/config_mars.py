# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

from dataclasses import dataclass, field

from lerobot.robots.config import RobotConfig

from .wire import DEFAULT_PORT_ACTIONS, DEFAULT_PORT_OBSERVATIONS, default_host


@RobotConfig.register_subclass("mars")
@dataclass
class MarsConfig(RobotConfig):
    # Hostname or IP of the robot running innate-os; `localhost` for the simulator or a lerobot
    # process on the Jetson itself. Defaults to $MARS_HOST so one export covers every command.
    remote_ip: str = field(default_factory=default_host)
    port_actions: int = DEFAULT_PORT_ACTIONS
    port_observations: int = DEFAULT_PORT_OBSERVATIONS
    connect_timeout_s: float = 5.0
    # How long get_observation() waits for a newer frame before returning the one it has. The
    # record loop paces itself at fps; waiting here on top of that halves the rate over Wi-Fi.
    poll_timeout_ms: int = 5
    # Head tilt the client sets at connect and holds for the session, so every frame of a dataset
    # sees the scene from the same angle; recorded in the dataset's meta/mars.json. None picks it
    # from the dataset behind the run (the one resumed or replayed, or the policy's training set)
    # and falls back to -20, the robot's own "AI position". hold_head=false leaves the head alone.
    head_angle_deg: float | None = None
    hold_head: bool = True
    head_tolerance_deg: float = 3.0
    head_reassert_s: float = 3.0
    # True while the phone app, the web app's teleop, or the leader arm drives the robot: send_action() is recorded but
    # not forwarded, so the client never echoes a stale command behind the operator.
    external_commands: bool = False
