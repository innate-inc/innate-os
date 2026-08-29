# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import logging
import time
from dataclasses import dataclass

import pytest

pytest.importorskip("rclpy")

from brain_client.state.joint_states import JointStates
from workspace.innate_skills.arm.pull_held_handle import PullHeldHandle


@dataclass
class _Pose:
    x: float = 0.2
    y: float = 0.0
    z: float = 0.2
    rpy: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def position(self):
        return self.x, self.y, self.z


class _Manipulation:
    def __init__(self):
        self.pose = _Pose()
        self.stops = 0

    def stream_to(self, x, y, z, **_kwargs):
        self.pose = _Pose(x, y, z)

    def stream_stop(self):
        self.stops += 1


def test_pull_held_handle_runs_bounded_stream_and_stops():
    skill = PullHeldHandle(logging.getLogger("pull-test"))
    skill.manipulation = _Manipulation()
    skill.joint_states = JointStates(
        name=("joint1", "joint2", "joint3", "joint4", "joint5", "joint6"),
        position=(0.0,) * 6,
        velocity=(0.0,) * 6,
        effort=(5.0, -4.0, 3.0, 2.0, 1.0, 40.0),
        received_at=time.monotonic(),
    )
    skill.sleep = lambda _seconds: None

    result = skill.execute(distance_m=0.012)

    assert "Pulled the held handle 0.01 m" in result
    assert skill.manipulation.pose.x == pytest.approx(0.188)
    assert skill.manipulation.stops == 1
