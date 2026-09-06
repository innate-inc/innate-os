import importlib
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workspace"))
from innate_skills.pickup_visual_action import fresh, trajectory, unchanged, validate_action


def action(kind="move", **kw):
    return dict(
        box_2d=[200, 200, 600, 600],
        action=kind,
        delta_xyz=[0, 0, -0.07] if kind == "move" else [0, 0, 0],
        delta_rpy=[0, 0.48, -0.24] if kind == "move" else [0, 0, 0],
        aligned=kind == "close",
        **kw,
    )


def test_bounded_actions_and_interpolated_pace():
    a = action()
    path = trajectory((0.30, 0, 0.10), (0, 0.82, 0.24), a)
    assert len(path) == 7
    assert path[-1][2] == pytest.approx(0.03)
    assert all(abs(p[2] - q[2]) <= 0.010001 for p, q in zip([(0, 0, 0.10)] + path, path, strict=False))
    for update in [
        dict(delta_xyz=[True, 0, 0]),
        dict(delta_xyz=[float("nan"), 0, 0]),
        dict(delta_xyz=[0.05, 0, 0]),
        dict(delta_rpy=[0, 0, 0.6]),
        dict(aligned=True),
        dict(action="dance"),
    ]:
        with pytest.raises(ValueError):
            validate_action({**a, **update})
    with pytest.raises(ValueError):
        trajectory((0.30, 0, 0.05), (0, 0.82, 0.24), a)
    with pytest.raises(ValueError):
        validate_action({**action("close"), "aligned": False})
    with pytest.raises(ValueError):
        validate_action({**action("close"), "delta_xyz": [0, 0, 0.001]})


def frame(image=None):
    im = np.full((480, 640), 60, np.uint8) if image is None else image
    return NS(
        jpeg=cv2.imencode(".jpg", im)[1].tobytes(),
        capture_ns=10_000_000_000,
        received_ros_ns=10_100_000_000,
        received_monotonic=20.0,
        capture_is_current=lambda: True,
    )


def test_capture_age_reset_and_local_scene_change():
    a = frame()
    assert fresh(a, 20.1)
    assert not fresh(a, 20.6)
    a.capture_ns = 9_000_000_000
    assert not fresh(a, 20.1)
    a = frame()
    a.capture_is_current = lambda: False
    assert not fresh(a, 20.1)
    assert unchanged(frame(), frame())
    im = np.full((480, 640), 60, np.uint8)
    im[200:220, 300:320] = 220
    assert not unchanged(frame(), frame(im))


@pytest.mark.parametrize("failure", [None, "abort", "changed", "cancel", "unreachable", "malformed"])
def test_real_grasp_path_move_then_close_or_open_abort(monkeypatch, failure):
    importlib.import_module("test_pick_any_object_retry")
    from innate_skills.pick_any_object_visual_action import PickAnyObjectVisualAction

    from innate.exceptions import SkillFailed

    s = object.__new__(PickAnyObjectVisualAction)
    calls = []
    s._grip_strength = 0.35
    s._search_clearance = "low"
    s._p = {"grasp_retries": 2, "floor_z": 0.03, "arm_pitch": 1.3}
    s._head_reference = None
    s.check_cancelled = lambda: None
    s.sleep = lambda _: None
    s._arm_joints = lambda: [0] * 5 + [0.85]
    s._prepare_wrist_search = lambda *_: None
    s.manipulation = NS(
        pose=NS(position=(0.3, 0, 0.1), rpy=(0, 0.82, 0.24)),
        GRIPPER_OPEN=0.85,
        torque_on=lambda: None,
        reachable=lambda *a, **k: failure != "unreachable",
    )

    def move(*xyz, **kw):
        calls.append("move")
        assert kw["duration"] == 0.5
        s.manipulation.pose = NS(position=xyz, rpy=(kw["roll"], kw["pitch"], kw["yaw"]))

    s.manipulation.move_to = move
    s._fresh_stationary_frame = lambda: frame()

    def stable(*_):
        if failure == "changed":
            raise SkillFailed("changed")
        if failure == "cancel":
            raise InterruptedError("stop")
        return frame()

    s._stable_after_decision = stable
    decisions = iter([action(), action("abort" if failure == "abort" else "close")])

    def locate(*a, **kw):
        if failure == "malformed":
            raise ValueError("malformed")
        return {"detections": [next(decisions)]}

    s._pickup_policy = NS(locate=locate)
    s._close_twist_lift = lambda *a: calls.append("close")
    if failure:
        with pytest.raises((SkillFailed, InterruptedError)):
            s._grasp_at("cube", (0.3, 0))
        assert "close" not in calls and s._metric_open_pregrasp
    else:
        s._grasp_at("cube", (0.3, 0))
        assert calls == ["move"] * 7 + ["close"]
        assert not s._metric_open_pregrasp


def test_real_post_inference_gate_rejects_changed_pose_reset_and_pixels(monkeypatch):
    importlib.import_module("test_pick_any_object_retry")
    from innate_skills.pick_any_object_visual_action import PickAnyObjectVisualAction

    from innate.exceptions import SkillFailed

    s = object.__new__(PickAnyObjectVisualAction)
    pose = NS(position=(0.3, 0, 0.04), rpy=(0, 0.82, 0.24))
    s.manipulation = NS(pose=pose)
    s.check_cancelled = lambda: None
    a = frame()
    b = frame()
    a.capture_generation = b.capture_generation = 1
    s._fresh_stationary_frame = lambda: b
    s._stable_after_decision(a, pose)
    b.capture_generation = 2
    with pytest.raises(SkillFailed):
        s._stable_after_decision(a, pose)
    b.capture_generation = 1
    s.manipulation.pose = NS(position=(0.31, 0, 0.04), rpy=pose.rpy)
    with pytest.raises(SkillFailed):
        s._stable_after_decision(a, pose)
    s.manipulation.pose = pose
    im = np.full((480, 640), 60, np.uint8)
    im[200:220, 300:320] = 220
    b.jpeg = frame(im).jpeg
    with pytest.raises(SkillFailed):
        s._stable_after_decision(a, pose)


def test_capture_wait_rejects_republished_old_image_and_honors_stop(monkeypatch):
    importlib.import_module("test_pick_any_object_retry")
    from innate_skills import pick_any_object_visual_action as module

    from innate.exceptions import SkillFailed

    s = object.__new__(module.PickAnyObjectVisualAction)
    s.wrist_image = frame()
    now = [20.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: now[0])

    def republish(_seconds):
        now[0] += 0.04
        s.wrist_image = frame()  # different object, identical old capture stamp

    s.sleep = republish
    with pytest.raises(SkillFailed, match="fresh captured"):
        s._fresh_stationary_frame()

    def stop(_seconds):
        raise InterruptedError("stop")

    s.sleep = stop
    with pytest.raises(InterruptedError):
        s._fresh_stationary_frame()


def test_scene_change_during_ik_preflight_cannot_issue_motion(monkeypatch):
    importlib.import_module("test_pick_any_object_retry")
    from innate_skills.pick_any_object_visual_action import PickAnyObjectVisualAction

    from innate.exceptions import SkillFailed

    s = object.__new__(PickAnyObjectVisualAction)
    s._grip_strength = 0.35
    s._search_clearance = "low"
    s._head_reference = None
    s._p = {"grasp_retries": 2, "floor_z": 0.03, "arm_pitch": 1.3}
    s.check_cancelled = lambda: None
    s._arm_joints = lambda: [0] * 5 + [0.85]
    s._prepare_wrist_search = lambda *_: None
    s._fresh_stationary_frame = frame
    s.sleep = lambda _: None
    changed = [False]
    moves = []

    def reachable(*a, **kw):
        changed[0] = True
        return True

    def stable(f, pose):
        if changed[0]:
            raise SkillFailed("scene changed during IK")
        return f

    s._stable_after_decision = stable
    s.manipulation = NS(
        pose=NS(position=(0.3, 0, 0.1), rpy=(0, 0.82, 0.24)),
        GRIPPER_OPEN=0.85,
        torque_on=lambda: None,
        reachable=reachable,
        move_to=lambda *a, **k: moves.append(a),
    )
    s._pickup_policy = NS(locate=lambda *a, **k: {"detections": [action()]})
    with pytest.raises(SkillFailed, match="during IK"):
        s._grasp_at("cube", (0.3, 0))
    assert not moves


@pytest.mark.parametrize(
    "position,rpy,expected",
    [
        ((0.3, 0, 0.074), (0, 0.83, 0.24), False),
        ((0.3, 0, 0.03), (0, 0.83, 0), False),
        ((0.3, 0, 0.03), (0, 1.3, 0.4), False),
        ((0.3, 0, 0.03), (0, 1.3, 0), True),
        ((0.3, 0, 0.031), (0.234, 1.308, 0.242), True),
    ],
)
def test_floor_posture_is_necessary_with_rotation_not_euler_error(position, rpy, expected):
    from innate_skills.pickup_visual_action import at_floor_grasp

    assert at_floor_grasp(position, rpy, {"floor_z": 0.03, "arm_pitch": 1.3}) is expected


@pytest.mark.parametrize("z,pitch", [(0.074, 1.3), (0.03, 0.83)])
def test_explicit_visual_close_cannot_bypass_mechanical_precondition(z, pitch):
    importlib.import_module("test_pick_any_object_retry")
    from innate_skills.pick_any_object_visual_action import PickAnyObjectVisualAction

    from innate.exceptions import SkillFailed

    s = object.__new__(PickAnyObjectVisualAction)
    s._p = {"floor_z": 0.03, "arm_pitch": 1.3}
    s._grip_strength = 0.35
    s._search_clearance = "low"
    s._head_reference = None
    s.sleep = lambda _: None
    s.check_cancelled = lambda: None
    s._prepare_wrist_search = lambda *_: None
    s._arm_joints = lambda: [0] * 5 + [0.85]
    s._fresh_stationary_frame = frame
    s._stable_after_decision = lambda f, p: f
    s.manipulation = NS(pose=NS(position=(0.3, 0, z), rpy=(0, pitch, 0)), GRIPPER_OPEN=0.85, torque_on=lambda: None)
    s._pickup_policy = NS(locate=lambda *a, **k: {"detections": [action("close")]})
    calls = []
    s._close_twist_lift = lambda *a: calls.append("close")
    with pytest.raises(SkillFailed, match="floor-grasp"):
        s._grasp_at("cube", (0.3, 0))
    assert not calls and s._metric_open_pregrasp


def test_latest_pose_cannot_drift_out_of_close_band_after_visual_check():
    importlib.import_module("test_pick_any_object_retry")
    from innate_skills.pick_any_object_visual_action import PickAnyObjectVisualAction

    from innate.exceptions import SkillFailed

    s = object.__new__(PickAnyObjectVisualAction)
    s._p = {"floor_z": 0.03, "arm_pitch": 1.3}
    s._grip_strength = 0.35
    s._search_clearance = "low"
    s._head_reference = None
    s.sleep = lambda _: None
    s.check_cancelled = lambda: None
    s._prepare_wrist_search = lambda *_: None
    s._fresh_stationary_frame = frame
    s._stable_after_decision = lambda f, p: f
    s.manipulation = NS(pose=NS(position=(0.3, 0, 0.037), rpy=(0, 1.3, 0)), GRIPPER_OPEN=0.85, torque_on=lambda: None)
    reads = [0]

    def joints():
        reads[0] += 1
        if reads[0] == 2:
            s.manipulation.pose = NS(position=(0.3, 0, 0.039), rpy=(0, 1.3, 0))
        return [0] * 5 + [0.85]

    s._arm_joints = joints
    s._pickup_policy = NS(locate=lambda *a, **k: {"detections": [action("close")]})
    calls = []
    s._close_twist_lift = lambda *a: calls.append("close")
    with pytest.raises(SkillFailed, match="floor-grasp"):
        s._grasp_at("cube", (0.3, 0))
    assert not calls


def test_visual_request_crops_only_identity_reference_and_remaps_box():
    import base64
    import json
    import time

    from innate_skills.pickup_policy import PickupPolicy

    bodies = []

    def transport(_model, body):
        bodies.append(body)
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 1},
                "output": [
                    {
                        "type": "function_call",
                        "name": "pickup_observation",
                        "arguments": json.dumps({"detections": [action("close")]}),
                    }
                ],
            },
        }

    im = np.full((300, 400, 3), 60, np.uint8)
    reference = {"image": base64.b64encode(cv2.imencode(".jpg", im)[1]).decode(), "box_2d": [300, 400, 700, 600]}
    result = PickupPolicy(transport).locate(
        "cube",
        "full-current-wrist",
        time.sleep,
        lambda: None,
        view="wrist_action",
        reference=reference,
        state={"position_xyz_m": [0.3, 0, 0.03]},
    )
    assert result["detections"][0]["action"] == "close"
    body = bodies[0]
    content = body["input"][0]["content"]
    context = json.loads(content[0]["text"])
    assert context["head_reference_is_padded_crop"]
    assert context["head_reference_box_2d"] == [250, 250, 750, 750]
    assert content[1]["image_url"].endswith(",full-current-wrist")
    jpeg = base64.b64decode(content[2]["image_url"].split(",")[1])
    assert cv2.imdecode(np.frombuffer(jpeg, np.uint8), 1).shape == (240, 160, 3)
    assert "Ignore the robot, fingers" not in body["instructions"]
    assert "floor_grasp_precondition" in body["instructions"]
    from innate_skills.pickup_visual_action import identity_reference

    invalid = {**reference, "box_2d": [0, 0, 0, 1]}
    assert identity_reference(invalid) is invalid
