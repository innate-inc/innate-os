"""Responses contract and native Skill integration without API or robot motion."""

import json
import logging
import threading
import time
from types import SimpleNamespace

import pytest
from innate_skills.arm.cabinet_agent_policy import CabinetPolicy, validate_action


def reply(action="observe", values=None):
    return {
        "status": "completed",
        "output": [
            {"type": "reasoning", "encrypted_content": "opaque"},
            {
                "type": "function_call",
                "name": "cabinet_action",
                "call_id": "c1",
                "arguments": json.dumps({"action": action, "values": values or [0, 0, 0], "note": "visible evidence"}),
            },
        ],
    }


def test_responses_history():
    sent = []
    policy = CabinetPolicy(transport=lambda p: sent.append(p) or reply())
    for step in range(3):
        call, action = policy.decide({"step": step}, {"head": "aGVhZA==", "wrist": "d3Jpc3Q="}, time.sleep)
        assert action[0] == "observe"
        policy.result(call, "observed")
    assert sent[0]["model"] == "gpt-6-astra"
    assert sent[0]["service_tier"] == "priority"
    assert not sent[0]["parallel_tool_calls"]
    assert sum(p["type"] == "input_image" for m in sent[2]["input"] for p in m.get("content", [])) == 4
    assert any(m.get("encrypted_content") == "opaque" for m in sent[2]["input"])
    assert any(m.get("type") == "function_call_output" for m in sent[2]["input"])


@pytest.mark.parametrize(
    "action,values",
    [
        ("base_step", [0.031, 0, 0]),
        ("base_turn", [0.13, 0, 0]),
        ("observe", [1, 0, 0]),
        ("move_wrist", [float("nan"), 0, 0]),
        ("move_wrist", [True, 0, 0]),
        ("exec_python", [0, 0, 0]),
    ],
)
def test_rejects_bad_actions(action, values):
    with pytest.raises(ValueError):
        validate_action({"action": action, "values": values, "note": "test"})


def test_stop_in_flight():
    pending, release = threading.Event(), threading.Event()

    class Stop(BaseException):
        pass

    def transport(_):
        pending.set()
        release.wait(2)
        return reply("base_step", [0.02, 0, 0])

    policy = CabinetPolicy(transport=transport)

    def cancel(_):
        assert pending.wait(1)
        raise Stop()

    try:
        with pytest.raises(Stop):
            policy.decide({}, {}, cancel)
        assert policy.calls == 0
    finally:
        release.set()


@pytest.mark.parametrize(
    "response",
    [
        {"status": "incomplete"},
        {"status": "completed", "output": []},
        {"status": "completed", "output": reply()["output"] * 2},
    ],
)
def test_incomplete_or_parallel_output(response):
    with pytest.raises((RuntimeError, ValueError)):
        CabinetPolicy(transport=lambda _: response).decide({}, {}, time.sleep)


@pytest.fixture(params=["gpt", "gemini"])
def native(monkeypatch, request):
    pytest.importorskip("rclpy")
    import importlib

    from innate import MainImage, WristImage

    module = importlib.import_module(f"innate_skills.arm.open_cabinet_with_{request.param}")
    cls = module.OpenCabinetWithGpt if request.param == "gpt" else module.OpenCabinetWithGemini
    skill = cls(logging.getLogger("cabinet-test"))
    calls = []
    skill.manipulation = SimpleNamespace(
        pose=SimpleNamespace(position=(0.24, 0, 0.25), rpy=(0, 0, 0)),
        torque_on=lambda: calls.append("torque"),
        gripper_open=lambda **_: calls.append("open"),
        gripper_close=lambda *a, **k: calls.append("close"),
        stream_stop=lambda: calls.append("arm_stop"),
    )
    skill.mobility = SimpleNamespace(stop=lambda: calls.append("base_stop"))
    skill.head = SimpleNamespace(set_position=lambda _: None)
    skill.main_image = MainImage.from_jpeg(b"head")
    skill.wrist_image = WristImage.from_jpeg(b"wrist")
    skill.joint_states = SimpleNamespace(name=("j1",) * 6, position=(0,) * 6, effort=(0,) * 6)
    monkeypatch.setattr(skill, "_effort", lambda: (0,) * 5)
    monkeypatch.setattr(skill, "_odom_xyt", lambda: (0, 0, 0))
    monkeypatch.setattr(skill, "_ensure_wrist_level", lambda: calls.append("level"))
    monkeypatch.setattr(skill, "_next_image", lambda camera, previous: type(previous).from_jpeg(camera.encode()))
    monkeypatch.setattr(skill, "_save_frame", lambda *a: None)
    monkeypatch.setattr(skill, "_move_wrist", lambda target, duration: calls.append("wrist"))
    monkeypatch.setattr(skill, "sleep", lambda _: None)
    monkeypatch.setattr(skill, "feedback", lambda *a: None)
    monkeypatch.setattr(module.OpenCabinetWithGpt, "_level_ik", SimpleNamespace(solve=lambda *a: None))
    return module, skill, calls


def test_native_registration_and_loop(native, monkeypatch):
    module, skill, calls = native
    from innate import Skill

    assert skill.name in ("open_cabinet_with_gpt", "open_cabinet_with_gemini")
    assert type(skill) in Skill._registry.values()
    sequence = iter([reply("close_gripper"), reply("base_turn", [0.1, 0, 0]), reply("open_gripper"), reply("done")])
    policy = CabinetPolicy(transport=lambda _: next(sequence))
    monkeypatch.setattr(skill, "_make_policy", lambda: policy)
    assert "visually reports" in skill.execute(max_steps=4)
    assert calls.count("level") == 4
    assert calls[-2:] == ["base_stop", "arm_stop"]
    assert any("Release gripper" in m.get("output", "") for m in policy.history)


def test_missing_key_no_motion(native, monkeypatch):
    _, skill, calls = native
    from innate.exceptions import SkillFailed

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("INNATE_SERVICE_KEY", raising=False)
    with pytest.raises(SkillFailed, match="OPENAI_API_KEY|INNATE_SERVICE_KEY"):
        skill.execute()
    assert calls == []


def test_unreachable_targets_do_not_move(native, monkeypatch):
    module, skill, calls = native
    with pytest.raises(ValueError, match="3 cm"):
        skill._act("move_wrist", (0.35, 0, 0.25))

    def reject(*_):
        raise ValueError("unreachable")

    monkeypatch.setattr(module.OpenCabinetWithGpt, "_level_ik", SimpleNamespace(solve=reject))
    with pytest.raises(ValueError, match="unreachable"):
        skill._act("move_wrist", (0.25, 0, 0.25))
    assert calls == []


def test_native_stop_cleans_up_before_late_reply(native, monkeypatch):
    module, skill, calls = native
    from innate.exceptions import SkillCancelled

    class CancelPolicy:
        def decide(self, *args):
            raise SkillCancelled()

    monkeypatch.setattr(skill, "_make_policy", CancelPolicy)
    with pytest.raises(SkillCancelled):
        skill.execute()
    assert calls[-2:] == ["base_stop", "arm_stop"]
    assert calls.count("open") == 1  # Staging only; cancellation never releases.


def test_budget_exhaustion_is_failure(native, monkeypatch):
    module, skill, calls = native
    from innate.exceptions import SkillFailed

    policy = CabinetPolicy(transport=lambda _: reply())
    monkeypatch.setattr(skill, "_make_policy", lambda: policy)
    with pytest.raises(SkillFailed, match="budget exhausted"):
        skill.execute(max_steps=1)
    assert calls[-2:] == ["base_stop", "arm_stop"]


def test_real_ik_respects_sim_shoulder_guard():
    pytest.importorskip("rclpy")
    import math

    import PyKDL as kdl
    from innate_skills.arm.level_handle_ik import LevelHandleIK

    planner = LevelHandleIK(shoulder_floor=-0.25)
    with pytest.raises(ValueError, match="No level"):
        planner.solve((0.24, 0.0, 0.25), [0.0] * 5)
    target = (0.30, 0.0, 0.30)
    joints = planner.solve(target, [0.0] * 5)
    assert joints[1] >= -0.25
    q, frame = kdl.JntArray(5), kdl.Frame()
    for i, value in enumerate(joints):
        q[i] = value
    planner.fk.JntToCart(q, frame)
    assert tuple(frame.p[i] for i in range(3)) == pytest.approx(target, abs=0.005)
    assert max(abs(v) for v in frame.M.GetRPY()[:2]) < math.radians(3)
