# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Exercise the shipped skill's dispatch and validation without moving hardware."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest


class Rejected(Exception):
    pass


@pytest.fixture
def skill(monkeypatch):
    class Skill:
        def fail(self, message):
            raise Rejected(message)

    api = types.ModuleType("innate")

    class Manipulation:
        GRIPPER_CLOSED = 0.0
        GRIPPER_OPEN = 0.85
        GRIPPER_MAX_STRENGTH = 0.6

    api.Skill, api.Manipulation, api.SkillReturn = Skill, Manipulation, str
    exceptions = types.ModuleType("innate.exceptions")
    exceptions.ArmFailed = type("ArmFailed", (Exception,), {})
    exceptions.ArmUnhealthy = type("ArmUnhealthy", (Exception,), {})
    monkeypatch.setitem(sys.modules, "innate", api)
    monkeypatch.setitem(sys.modules, "innate.exceptions", exceptions)
    path = Path(__file__).resolve().parents[1] / "workspace/innate_skills/arm/arm_move.py"
    spec = importlib.util.spec_from_file_location("arm_move_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = module.ArmMove()
    value.manipulation = Mock()
    return value, exceptions


def test_xyz_uses_ik_api(skill):
    value, _ = skill
    value.execute("xyz", x=0.2, y=0.1, z=0.3, pitch=0.5, duration=2)
    value.manipulation.move_to.assert_called_once_with(
        0.2, 0.1, 0.3, roll=0.0, pitch=0.5, yaw=0.0, duration=2, tolerance_xy=None, tolerance_z=None
    )
    value.manipulation.move_joints.assert_not_called()


@pytest.mark.parametrize("count", [5, 6])
def test_joint_targets_preserve_gripper_convention(skill, count):
    value, _ = skill
    joints = [0.1] * count
    value.execute("joints", joints=joints)
    value.manipulation.move_joints.assert_called_once_with(joints, duration=3)
    value.manipulation.move_to.assert_not_called()


@pytest.mark.parametrize(
    "args",
    [
        {"mode": "unknown"},
        {"mode": "xyz"},
        {"mode": "xyz", "x": 0, "y": 0},
        {"mode": "xyz", "x": 0, "y": 0, "z": float("nan")},
        {"mode": "xyz", "x": 0, "y": 0, "z": 0, "joints": [0] * 5},
        {"mode": "joints"},
        {"mode": "joints", "joints": [0] * 4},
        {"mode": "joints", "joints": [0] * 7},
        {"mode": "joints", "joints": "00000"},
        {"mode": "joints", "joints": [0] * 4 + [float("inf")]},
        {"mode": "joints", "joints": [0] * 4 + [True]},
        {"mode": "joints", "joints": [0] * 5, "x": 0},
        {"mode": "joints", "joints": [0] * 5, "roll": 1},
        {"mode": "joints", "joints": [0] * 5, "duration": 0},
        {"mode": "joints", "joints": [0] * 5, "duration": -1},
        {"mode": "joints", "joints": [0] * 5, "duration": float("nan")},
    ],
)
def test_invalid_inputs_never_move(skill, args):
    value, _ = skill
    with pytest.raises(Rejected):
        value.execute(**args)
    assert not value.manipulation.mock_calls


@pytest.mark.parametrize("mode,method", [("xyz", "move_to"), ("joints", "move_joints")])
@pytest.mark.parametrize("error", ["ArmFailed", "ArmUnhealthy"])
def test_motion_failure_is_reported(skill, mode, method, error):
    value, exceptions = skill
    getattr(value.manipulation, method).side_effect = getattr(exceptions, error)("unreachable")
    args = {"x": 0.2, "y": 0.1, "z": 0.3} if mode == "xyz" else {"joints": [0] * 5}
    with pytest.raises(Rejected, match="unreachable"):
        value.execute(mode, **args)


@pytest.mark.parametrize("mode,method", [("xyz", "move_to"), ("joints", "move_joints")])
def test_cancellation_propagates_without_retry(skill, mode, method):
    class Cancelled(BaseException):
        pass

    value, _ = skill
    command = getattr(value.manipulation, method)
    command.side_effect = Cancelled()
    args = {"x": 0.2, "y": 0.1, "z": 0.3} if mode == "xyz" else {"joints": [0] * 5}
    with pytest.raises(Cancelled):
        value.execute(mode, **args)
    assert command.call_count == 1


@pytest.mark.parametrize("target", [-0.600001, 0.850001, -10.0, 10.0])
def test_unsafe_gripper_target_rejects_entire_motion(skill, target):
    value, _ = skill
    with pytest.raises(Rejected, match="joint6"):
        value.execute("joints", joints=[0.1] * 5 + [target])
    assert not value.manipulation.mock_calls


@pytest.mark.parametrize("target", [-0.6, 0.0, 0.85])
def test_gripper_boundaries_are_allowed(skill, target):
    value, _ = skill
    joints = [0.1] * 5 + [target]
    value.execute("joints", joints=joints)
    value.manipulation.move_joints.assert_called_once_with(joints, duration=3.0)
