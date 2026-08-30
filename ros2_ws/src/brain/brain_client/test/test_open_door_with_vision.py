# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import importlib
import logging
import math
import sys
from pathlib import Path

import pytest

pytest.importorskip("rclpy")

from brain_client.skills.types import SkillOutput

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
geometry = importlib.import_module("workspace.innate_skills.arm.handle_triangulation")
module = importlib.import_module("workspace.innate_skills.arm.open_door_with_vision")
OpenDoorWithVision = module.OpenDoorWithVision


def _unit(vector):
    norm = math.sqrt(sum(value * value for value in vector))
    return tuple(value / norm for value in vector)


def test_triangulates_intersecting_forward_rays():
    target = (0.8, 0.1, 0.2)
    first_origin = (0.0, 0.0, 0.26)
    second_origin = (-0.1, -0.08, 0.26)
    first = first_origin, _unit(tuple(target[i] - first_origin[i] for i in range(3)))
    second = second_origin, _unit(tuple(target[i] - second_origin[i] for i in range(3)))

    point, gap, angle = geometry.triangulate_rays(first, second)

    assert point == pytest.approx(target)
    assert gap == pytest.approx(0.0, abs=1e-9)
    assert angle > 3.0


def test_triangulation_rejects_parallel_rays():
    with pytest.raises(ValueError, match="angle too small"):
        geometry.triangulate_rays(((0, 0, 0), (1, 0, 0)), ((0, 0.1, 0), (1, 0, 0)))


def test_camera_ray_is_normalized_and_transformed_to_odom():
    origin, direction = geometry.camera_ray_odom(320, 240, 0.0, (1.0, 2.0, math.pi / 2.0))

    assert origin[0] == pytest.approx(1.0 - 0.0295, abs=0.002)
    assert origin[1] == pytest.approx(2.0 + 0.0025, abs=0.002)
    assert math.sqrt(sum(value * value for value in direction)) == pytest.approx(1.0)
    assert direction[1] > 0.99


class _Mobility:
    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1


class _Manipulation:
    def __init__(self):
        self.stops = 0
        self.setup = []

    def torque_on(self):
        self.setup.append("torque_on")

    def gripper_open(self, **_kwargs):
        self.setup.append("gripper_open")

    def move_joints(self, joints, **_kwargs):
        self.setup.append(("move_joints", joints))

    def stream_stop(self):
        self.stops += 1


class _Head:
    def __init__(self):
        self.positions = []

    def set_position(self, value):
        self.positions.append(value)


class _Skills:
    def __init__(self):
        self.calls = []

    def run(self, skill_id, **inputs):
        self.calls.append((skill_id, inputs))
        return SkillOutput("pulled")


def test_full_skill_acquires_before_pull_handoff(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("open-door-test"))
    skill.mobility = _Mobility()
    skill.manipulation = _Manipulation()
    skill.head = _Head()
    skill.skills = _Skills()
    order = []
    monkeypatch.setattr(skill, "_triangulate_handle", lambda: order.append("triangulate") or (1.0, 0.0, 0.2))
    monkeypatch.setattr(skill, "_position_base", lambda _point: order.append("position") or (0.34, 0.0, 0.2))
    monkeypatch.setattr(skill, "_wrist_align", lambda target: order.append("align") or (0.28, target[1], target[2]))
    monkeypatch.setattr(skill, "_grasp", lambda _pregrasp, _handle_x: order.append("grasp"))

    result = skill.execute(pull_distance_m=0.03)

    assert order == ["triangulate", "position", "align", "grasp"]
    assert "0.03 m" in result
    assert skill.skills.calls == [
        (
            "innate-os/pull_held_handle",
            {
                "timeout": 35.0,
                "distance_m": 0.03,
                "direction_x": -1.0,
                "direction_y": 0.0,
                "direction_z": 0.0,
            },
        )
    ]
    assert skill.mobility.stops == 1
    assert skill.manipulation.stops == 1
    assert skill.manipulation.setup[:2] == ["torque_on", "gripper_open"]
    assert skill.head.positions == [0, 0]
