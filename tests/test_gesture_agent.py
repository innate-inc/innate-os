"""Phase selection, inspection and proposal validation; never actuates hardware."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for directory in ("ros2_ws/src/brain/brain_client", "ros2_ws/src/mars_bot/mars_arm", "workspace"):
    sys.path.insert(0, str(ROOT / directory))
from innate.gesture import Gesture, validate_action  # noqa: E402
from innate.gesture_agent import DemonstrationAgentPolicy  # noqa: E402


def frame(i):
    return {"index": i, "images": {"head": "a", "wrist": "b"}, "time_s": i / 30}


def policy():
    p = DemonstrationAgentPolicy.__new__(DemonstrationAgentPolicy)
    p.demo = SimpleNamespace(path="test.h5", poses=[None] * 100)
    p.frames = {i: frame(i) for i in [0, 20, 40, 60, 99]}
    p.overview = set(p.frames)
    p.phase_map = []
    p.phase = 0
    p.inspected_detail = False
    return p


def phases():
    return {
        "phases": [
            {
                "name": "align",
                "start_frame": 0,
                "end_frame": 40,
                "reference_frame": 20,
                "advance_when": "battery centered",
            },
            {
                "name": "grasp and present",
                "start_frame": 40,
                "end_frame": 99,
                "reference_frame": 60,
                "advance_when": "battery held forward",
            },
        ]
    }


def proposal():
    return {
        "phase": 0,
        "previous_phase_complete": False,
        "evidence": "battery needs alignment",
        "decision": {
            "action": "observe",
            "reference_frame": 20,
            "pose": [0.3, 0, 0.2, 0, 0, 0],
            "reason": "Inspect alignment",
            "safe": True,
            "holding": False,
            "presented": False,
        },
    }


def test_inspect_plan_act_integration(monkeypatch):
    p = policy()
    import innate.gesture_agent as module

    monkeypatch.setattr(
        module,
        "Gesture",
        lambda path, frame_indices, **kwargs: SimpleNamespace(frames=[frame(i) for i in frame_indices]),
    )
    calls = iter([("inspect_demo", {"frames": [20, 60]}), ("record_phases", phases()), ("act", proposal())])
    contexts = []

    def request(content):
        contexts.append(content)
        return next(calls)

    p._request = request
    observation = {"pose": [0.3, 0, 0.2, 0, 0, 0], "images": {"head": "live"}}
    result = p.decide(observation, [])
    assert validate_action(result, observation["pose"], 100, False) == result
    assert len(p.phase_map) == 2
    assert [x["tool"] for x in p.last_trace] == ["inspect_demo", "record_phases", "act"]
    assert "phase_map" in str(contexts[-1])
    assert "LIVE head" in str(contexts[-1])


def test_reject_uninspected_map_and_phase_skip():
    p = policy()
    with pytest.raises(ValueError, match="Inspect detailed"):
        p._record_phases(phases())
    p.inspected_detail = True
    invalid = phases()
    invalid["phases"][0]["reference_frame"] = 19
    with pytest.raises(ValueError, match="inspected"):
        p._record_phases(invalid)
    p._record_phases(phases())
    value = proposal()
    value["phase"] = 2
    with pytest.raises(ValueError, match="skip"):
        p._action(value, [1])
    value["phase"] = 1
    with pytest.raises(ValueError, match="evidence"):
        p._action(value, [1])
    value["previous_phase_complete"] = True
    value["decision"]["reference_frame"] = 60
    assert p._action(value, [1])["reference_frame"] == 60
    assert p.phase == 1


def test_no_action_without_plan_or_with_wrong_reference():
    p = policy()
    with pytest.raises(ValueError, match="map required"):
        p._action(proposal(), [])
    p.inspected_detail = True
    p._record_phases(phases())
    value = proposal()
    value["decision"]["reference_frame"] = 60
    with pytest.raises(ValueError, match="in its phase"):
        p._action(value, [])
    value = proposal()
    value["decision"]["action"] = "done"
    with pytest.raises(ValueError, match="final phase"):
        p._action(value, [])


def test_inspection_budget_never_returns_motion(monkeypatch):
    p = policy()
    import innate.gesture_agent as module

    monkeypatch.setattr(
        module,
        "Gesture",
        lambda path, frame_indices, **kwargs: SimpleNamespace(frames=[frame(i) for i in frame_indices]),
    )
    p._request = lambda _: ("inspect_demo", {"frames": [20]})
    with pytest.raises(ValueError, match="budget exhausted"):
        p.decide({"images": {}}, [])


def test_actual_episode_arbitrary_frames():
    path = os.environ.get("DEMO_EPISODE")
    if not path:
        pytest.skip("Set DEMO_EPISODE for a read-only real-recording integration check")
    demo = Gesture(path, frame_indices=[1, 178, 239, 364], image_time_reference=True)
    assert [f["index"] for f in demo.frames] == [1, 178, 239, 364]
    assert len(demo.context()) == 20
    wrist = demo.frames[1]["camera_observations"]["wrist"]
    assert wrist["row_offset_s"] < -0.25
    assert wrist["source_arm_index"] < 178
    with pytest.raises(ValueError, match="not synchronized"):
        Gesture(path, frame_indices=[178])
    for invalid in [[], [-1], [99999], [True], list(range(9))]:
        with pytest.raises(ValueError):
            Gesture(path, frame_indices=invalid)


@pytest.mark.parametrize("mode", ["success", "cancel_after_close", "unreachable", "model_failure"])
def test_execution_loop_with_real_demo_and_fake_actuators(tmp_path, monkeypatch, mode):
    path = os.environ.get("DEMO_EPISODE")
    if not path:
        pytest.skip("Set DEMO_EPISODE for the real-episode execution harness")
    import test_gesture_imitation as existing

    monkeypatch.setattr(existing, "Gesture", lambda *a, **kw: Gesture(*a, **kw, image_time_reference=True))
    existing.test_real_skill_loop_preserves_grip_and_stops_on_failure(Path(path), tmp_path, monkeypatch, mode)


def test_battery_skill_refuses_existing_grasp():
    from innate_skills.pick_and_hand_battery_agent import PickAndHandBatteryAgent

    from innate.exceptions import SkillFailed

    skill = PickAndHandBatteryAgent(None)
    skill.manipulation = SimpleNamespace(pose=SimpleNamespace(gripper=0.4))
    with pytest.raises(SkillFailed, match="open empty gripper"):
        skill.execute()


@pytest.mark.parametrize("reachable,miss", [(False, False), (True, True), (True, False)])
def test_motion_feedback_contains_actual_state(reachable, miss):
    from innate_skills.imitate_pick_and_present import ImitatePickAndPresent

    skill = ImitatePickAndPresent(None)
    commands = []
    skill.manipulation = SimpleNamespace(
        reachable=lambda *a, **kw: reachable, move_to=lambda *a, **kw: commands.append(a)
    )
    measured = {"pose": [0.28 if miss else 0.30, 0, 0.2, 0, 0, 0], "qpos": [0] * 6}
    skill._wait_motion = lambda *a: None
    skill._observe = lambda *a: measured
    value = skill._try_move([0.30, 0, 0.2, 0, 0, 0], measured, None, "")
    assert value["status"] == ("unreachable" if not reachable else "not_reached" if miss else "reached")
    assert value["measured_pose"] == measured["pose"]
    assert len(commands) == int(reachable)


def test_rejected_target_reaches_next_agent_turn(tmp_path, monkeypatch):
    import copy

    import ament_index_python.packages
    from innate_skills import imitate_pick_and_present as runtime

    from innate.exceptions import SkillCancelled

    monkeypatch.setattr(
        ament_index_python.packages,
        "get_package_share_directory",
        lambda _: str(ROOT / "ros2_ws/src/mars_bot/mars_sim"),
    )
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    demo = SimpleNamespace(
        path="episode.h5",
        poses=[None] * 100,
        model_hash=__import__("hashlib")
        .sha256((ROOT / "ros2_ws/src/mars_bot/mars_sim/urdf/mars.urdf").read_bytes())
        .hexdigest(),
    )
    skill = runtime.ImitatePickAndPresent(None)
    skill.make_demo = lambda *a: demo
    skill.make_policy = lambda *a: object()
    monkeypatch.setattr(runtime, "LiveGestureObservation", lambda: SimpleNamespace(close=lambda: None))
    current = {"pose": [0.3, 0, 0.2, 0, 0, 0], "base": [0, 0, 0], "images": {}, "gripper": 1.0}
    skill._observe = lambda *a: copy.deepcopy(current)
    skill.head_position = SimpleNamespace(pitch_degrees=0)
    skill.mobility = SimpleNamespace(stop=lambda: None)
    skill.manipulation = SimpleNamespace(
        safety=SimpleNamespace(max_ee_speed=None), reachable=lambda *a, **kw: False, halt=lambda: None
    )
    skill.sleep = lambda _: None
    skill.feedback = lambda _: None
    observed = []

    def decide(policy, observation, history):
        if history:
            observed.append(copy.deepcopy(history[-1]["execution"]))
            raise SkillCancelled()
        value = proposal()["decision"]
        value["action"] = "move"
        value["pose"][0] = 0.32
        return value

    skill._decide = decide
    with pytest.raises(SkillCancelled):
        skill.execute("episode.h5")
    assert observed[0]["status"] == "unreachable"
    assert observed[0]["requested_pose"][0] == 0.32
    assert observed[0]["measured_pose"][0] == 0.3


def test_hardware_fault_is_distinct_from_torque_off():
    from innate_skills.imitate_pick_and_present import LiveGestureObservation, ServoHardwareFault

    with pytest.raises(ServoHardwareFault):
        LiveGestureObservation.check_health(
            SimpleNamespace(is_ok=False, is_torque_enabled=True, error="Servo 2 hardware error: overload")
        )
    with pytest.raises(ValueError) as error:
        LiveGestureObservation.check_health(SimpleNamespace(is_ok=True, is_torque_enabled=False, error=""))
    assert not isinstance(error.value, ServoHardwareFault)


@pytest.mark.parametrize("success,status", [(True, "fixed"), (True, "no_errors"), (False, "error")])
def test_targeted_reload_service(success, status):
    import json
    from innate_skills.imitate_pick_and_present import LiveGestureObservation

    calls = []
    monitor = LiveGestureObservation.__new__(LiveGestureObservation)
    monitor.fix_error = SimpleNamespace(
        service_is_ready=lambda: True,
        call_async=lambda request: (
            calls.append(request)
            or SimpleNamespace(
                done=lambda: True,
                result=lambda: SimpleNamespace(
                    success=success, message=json.dumps({"status": status, "error_ids": [2]})
                ),
            )
        ),
    )

    def fail(message):
        raise RuntimeError(message)

    skill = SimpleNamespace(check_cancelled=lambda: None, fail=fail)
    if success:
        assert monitor.reload_failed_servos(skill)["error_ids"] == [2]
    else:
        with pytest.raises(RuntimeError, match="recovery failed"):
            monitor.reload_failed_servos(skill)
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["recovered", "budget", "cancel"])
def test_fault_during_close_keeps_grasp_and_replans(monkeypatch, tmp_path, mode):
    import copy
    import hashlib
    import ament_index_python.packages
    from innate.exceptions import SkillCancelled
    from innate_skills import imitate_pick_and_present as runtime

    monkeypatch.setattr(
        ament_index_python.packages,
        "get_package_share_directory",
        lambda _: str(ROOT / "ros2_ws/src/mars_bot/mars_sim"),
    )
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    demo = SimpleNamespace(
        path="episode.h5",
        poses=[None] * 100,
        model_hash=hashlib.sha256((ROOT / "ros2_ws/src/mars_bot/mars_sim/urdf/mars.urdf").read_bytes()).hexdigest(),
    )
    skill = runtime.ImitatePickAndPresent(None)
    skill.max_servo_recoveries = 2
    skill.make_demo = lambda *a: demo
    skill.make_policy = lambda *a: object()
    monkeypatch.setattr(runtime, "LiveGestureObservation", lambda: SimpleNamespace(close=lambda: None))
    current = {"pose": [0.3, 0, 0.2, 0, 0, 0], "base": [0, 0, 0], "images": {}, "gripper": 1.0}
    skill._observe = lambda *a: copy.deepcopy(current)
    skill.head_position = SimpleNamespace(pitch_degrees=0)
    skill.mobility = SimpleNamespace(stop=lambda: None)
    closed = []
    skill.manipulation = SimpleNamespace(
        safety=SimpleNamespace(max_ee_speed=None), halt=lambda: None, gripper_close=lambda **kw: closed.append(kw)
    )
    skill.sleep = lambda _: None
    skill.feedback = lambda _: None

    def fault(*args):
        raise runtime.ServoHardwareFault("Servo 2 hardware error: overload")

    skill._wait_motion = fault
    recoveries = []

    def recover(*args):
        if mode == "cancel":
            raise SkillCancelled()
        recoveries.append(True)
        current["pose"][2] = 0.18
        return {"status": "servo_recovered", "servo_ids": [2], "measured_pose": current["pose"]}

    skill._recover_servo = recover

    def decide(policy, observation, history):
        if history:
            assert observation["grasp_committed"] is True
            assert history[-1]["execution"]["servo_ids"] == [2]
            assert observation["pose"][2] == 0.18
            if mode == "budget":
                fault()
            raise SkillCancelled()
        value = proposal()["decision"]
        value["action"] = "close"
        return value

    skill._decide = decide
    if mode == "budget":
        with pytest.raises(Exception, match="recovery budget exhausted"):
            skill.execute("episode.h5")
        assert len(recoveries) == 2
    else:
        with pytest.raises(SkillCancelled):
            skill.execute("episode.h5")
        assert len(recoveries) == (0 if mode == "cancel" else 1)
    assert len(closed) == 1  # no reopened grip or replayed close


@pytest.mark.parametrize("mode", ["healthy", "unhealthy", "cancel"])
def test_recovery_requires_new_healthy_status(monkeypatch, mode):
    import threading
    from innate.exceptions import SkillCancelled
    from innate_skills import imitate_pick_and_present as runtime

    ticks = [10.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: ticks[0])
    skill = runtime.ImitatePickAndPresent(None)
    calls = []
    skill.feedback = lambda _: None
    skill.manipulation = SimpleNamespace(
        moving=False, halt=lambda: calls.append("halt"), wait=lambda **kw: calls.append("wait")
    )
    monitor = SimpleNamespace(
        lock=threading.Lock(), values={"health": (SimpleNamespace(is_ok=True, is_torque_enabled=True), 9.0, None)}
    )
    monitor.reload_failed_servos = lambda _: calls.append("fix_error") or {"error_ids": [2]}

    def sleep(_):
        if mode == "cancel":
            raise SkillCancelled()
        ticks[0] += 1
        if mode == "healthy":
            monitor.values["health"] = (SimpleNamespace(is_ok=True, is_torque_enabled=True), ticks[0], None)

    skill.sleep = sleep
    skill._observe = lambda *a: {"pose": [0.3, 0, 0.18, 0, 0, 0], "qpos": [1] * 6}
    if mode == "healthy":
        result = skill._recover_servo(monitor, "xml", "overload")
        assert result["servo_ids"] == [2]
        assert result["measured_pose"][2] == 0.18
        assert calls == ["halt", "fix_error", "wait"]
    else:
        with pytest.raises(SkillCancelled if mode == "cancel" else Exception):
            skill._recover_servo(monitor, "xml", "overload")
        assert calls == ["halt", "fix_error"]


def test_reload_timeout_does_not_retry(monkeypatch):
    from innate_skills import imitate_pick_and_present as runtime

    ticks = [0.0]
    monkeypatch.setattr(runtime.time, "monotonic", lambda: ticks[0])
    calls = []
    monitor = runtime.LiveGestureObservation.__new__(runtime.LiveGestureObservation)
    monitor.fix_error = SimpleNamespace(
        service_is_ready=lambda: True,
        call_async=lambda request: calls.append(request) or SimpleNamespace(done=lambda: False),
    )

    def sleep(_):
        ticks[0] += 1

    def fail(message):
        raise RuntimeError(message)

    skill = SimpleNamespace(check_cancelled=lambda: None, sleep=sleep, fail=fail)
    with pytest.raises(RuntimeError, match="outcome unknown"):
        monitor.reload_failed_servos(skill)
    assert len(calls) == 1
