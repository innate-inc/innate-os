# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Exercise the real skill with recording hardware adapters; no ROS or motion."""

import importlib.util
import math
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def skill_module(monkeypatch):
    # Only the dependency surface is stubbed: all skill decisions run unchanged.
    sdk = ModuleType("innate")
    for name in (
        "Arm",
        "Head",
        "HeadState",
        "Llm",
        "MainImage",
        "Mobility",
        "Odometry",
        "Skill",
        "SkillReturn",
        "WristImage",
    ):
        setattr(sdk, name, type(name, (), {}))
    sdk.Manipulation = SimpleNamespace(REACH_X=(0.22, 0.40), REACH_Y=(-0.10, 0.10))
    llm = ModuleType("innate_llm")
    for name in ("Image", "Message", "Request", "Role", "Text", "Thinking"):
        setattr(llm, name, object)
    exceptions = ModuleType("innate.exceptions")
    exceptions.ArmFailed = type("ArmFailed", (Exception,), {})
    exceptions.ArmUnhealthy = type("ArmUnhealthy", (Exception,), {})
    spec = importlib.util.spec_from_file_location(
        "geometry", ROOT / "ros2_ws/src/brain/brain_client/innate/geometry.py"
    )
    geometry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(geometry)
    for name, module in {
        "innate": sdk,
        "innate.exceptions": exceptions,
        "innate.geometry": geometry,
        "innate_llm": llm,
        "cv2": ModuleType("cv2"),
        "numpy": SimpleNamespace(arange=lambda *args: [], ndarray=object),
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("do_task_under_test", ROOT / "workspace/innate_skills/do_task.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_skill(module, x=0.28, z=0.1):
    skill = module.DoTask()
    skill.arm = SimpleNamespace(x=x, y=0.0, z=z)
    skill.manipulation = Mock()
    skill._rpy = (0.0, 0.0, 0.0)
    skill.sleep = lambda _: None
    skill._arm_report = lambda _: "measured pose"
    skill._grip_state = lambda: "closed"
    return skill


def test_exact_boundary_and_existing_other_bounds(skill_module):
    m = skill_module
    assert m._out_of_reach(0.30, 0) is None
    for x, y in [(0.300001, 0), (0.31, 0), (0.40, 0), (math.inf, 0), (math.nan, 0), (0.20, 0), (0.28, 0.12)]:
        assert "nothing moved" in m._out_of_reach(x, y)
    assert m.Manipulation.REACH_X == (0.22, 0.40)  # other skills unaffected


def test_nudge_rejects_overreach_without_commanding(skill_module):
    s = make_skill(skill_module)
    assert "nothing moved" in s._nudge({"dx": 0.04})
    s.manipulation.move_to.assert_not_called()
    s._nudge({"dx": -0.02})
    assert math.isclose(s.manipulation.move_to.call_args.args[0], 0.26)


def test_unfold_uses_compact_cartesian_target(skill_module):
    s = make_skill(skill_module, x=0.1)
    s._nudge({"dx": 0.05})
    assert s.manipulation.move_to.call_args.args == (0.30, 0.0, 0.22)
    s.manipulation.move_joints.assert_not_called()


def test_all_cartesian_commands_have_backstop(skill_module):
    s = make_skill(skill_module)
    for x in [0.300001, 0.40, math.nan, math.inf]:
        with pytest.raises(skill_module.ArmFailed):
            s._move_to(x, 0, 0.2)
    s.manipulation.move_to.assert_not_called()
    s._move_to(0.30, 0, 0.2)
    s.manipulation.move_to.assert_called_once()


def test_lift_and_fold_retract_an_already_extended_arm(skill_module):
    s = make_skill(skill_module, x=0.40, z=0.02)
    s._grip({"close": True})
    assert s.manipulation.move_to.call_args.args[0] == 0.30
    s._fold()
    assert s.manipulation.move_to.call_args.args == (0.30, 0.0, 0.22)
    s.manipulation.move_joints.assert_called_once_with(skill_module.NAV_ARM, duration=2.0)


def test_model_reach_box_matches_skill_limit(skill_module, monkeypatch):
    m = skill_module
    s = make_skill(m)
    s.main_image = SimpleNamespace(jpeg=b"image")
    s.head_position = SimpleNamespace(pitch_degrees=0)
    lines = []
    monkeypatch.setattr(m, "_decode", lambda _: "image")
    monkeypatch.setattr(m, "_encode", lambda image: image)
    monkeypatch.setattr(m, "floor_to_pixel", lambda x, y, tilt: (x, y))
    monkeypatch.setattr(m, "_polyline", lambda image, points, *args: lines.append(points))
    s._head_view()
    assert math.isclose(max(x for x, y in lines[-1]), 0.29)  # fingertip offset
    assert "x <= 0.30 m" in m.SYSTEM
