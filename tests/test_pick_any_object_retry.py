import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workspace"))

# The host-side CI intentionally does not install the ROS-backed innate SDK.
# This test exercises only the grasp state machine, so provide its import-time
# types without pretending to implement any robot behavior.
innate = ModuleType("innate")
innate.Skill = object
innate.SkillReturn = type("SkillReturn", (), {})
for resource_type in ("Head", "JointStates", "MainImage", "Manipulation", "Mobility", "Odometry", "WristImage"):
    setattr(innate, resource_type, type(resource_type, (), {}))


class _Waypoint:
    def __init__(self, _x, _y, z, **_kwargs):
        self.z = z


innate.Waypoint = _Waypoint
innate.resource = lambda function: function
innate.gemini = ModuleType("innate.gemini")
innate.vision = ModuleType("innate.vision")
innate.vision.Axis = type("Axis", (), {})
sys.modules["innate"] = innate
sys.modules["innate.gemini"] = innate.gemini
sys.modules["innate.vision"] = innate.vision

exceptions = ModuleType("innate.exceptions")
for exception_type in ("ArmFailed", "ArmUnhealthy", "SkillFailed"):
    setattr(exceptions, exception_type, type(exception_type, (Exception,), {}))
sys.modules["innate.exceptions"] = exceptions

geometry = ModuleType("innate.geometry")
geometry.pixel_to_floor = lambda *_args: None
sys.modules["innate.geometry"] = geometry

approach = ModuleType("innate_skills.approach")
approach.APPROACH_PARAMS = {"settle_s": 0.0, "tilt_deg": 35.0}
approach.FloorApproach = type("FloorApproach", (), {})
approach.ask_head = lambda *_args, **_kwargs: (None, None)
approach.base_to_odom = lambda *_args: None
approach.inside_box = lambda *_args: False
sys.modules["innate_skills.approach"] = approach

from innate_skills.pick_any_object import PARAMS, PickAnyObject  # noqa: E402


class _Manipulation:
    GRIPPER_OPEN = 0.8

    def __init__(self, skill, *, closes_empty, empties_on_lift=False):
        self.skill = skill
        self.pose = SimpleNamespace(z=0.04)
        self.closes_empty = closes_empty
        self.empties_on_lift = empties_on_lift
        self.events = []

    def gripper_open(self, duration):
        self.events.append(("open", duration))

    def gripper_close(self, strength, duration):
        self.events.append(("close", strength, duration))
        self.skill.joint_states.position[5] = -0.085 if self.closes_empty else 0.1

    def follow(self, waypoints, grip):
        self.events.append(("follow", [wp.z for wp in waypoints], grip))
        self.pose.z = waypoints[-1].z

    def move_to(self, _x, _y, z, **_kwargs):
        self.events.append(("move_to", z))
        self.pose.z = z

    def move_joints(self, _joints, duration):
        self.events.append(("lift", duration))
        self.pose.z = 0.22
        if self.empties_on_lift:
            self.skill.joint_states.position[5] = -0.085


class _Approach:
    def __init__(self):
        self.moves = []

    def drive(self, distance):
        self.moves.append(distance)


def _skill(*, closes_empty, empties_on_lift=False):
    skill = PickAnyObject.__new__(PickAnyObject)
    skill._p = dict(PARAMS)
    skill.joint_states = SimpleNamespace(position=[0.0] * 6)
    skill.logger = SimpleNamespace(info=lambda *_args: None, warning=lambda *_args: None)
    skill.check_cancelled = lambda: None
    skill._grip_strength = 0.0
    skill._holding = False
    skill.manipulation = _Manipulation(skill, closes_empty=closes_empty, empties_on_lift=empties_on_lift)
    skill._goto_search_pose = lambda _bearing: skill.manipulation.events.append(("search",))
    skill._wrist_descend = lambda _prompt, x, y: (x - 0.01, y + 0.01, 0.05, 0.0)
    skill._grasp_orientation = lambda _x, _y, roll: (roll, 1.3, 0.0)
    return skill


def test_empty_grasp_recenters_twice_but_held_object_does_not_retry(monkeypatch):
    monkeypatch.setattr("innate_skills.pick_any_object.time.sleep", lambda _seconds: None)

    missed = _skill(closes_empty=True)
    missed._close_twist_lift("brick", 0.25, 0.0, 0.0, 1.3, 0.0)
    assert [event[0] for event in missed.manipulation.events].count("open") == 2
    assert [event[0] for event in missed.manipulation.events].count("search") == 2
    assert [event[0] for event in missed.manipulation.events].count("close") == 3

    held = _skill(closes_empty=False)
    held._close_twist_lift("brick", 0.25, 0.0, 0.0, 1.3, 0.0)
    assert [event[0] for event in held.manipulation.events].count("open") == 0
    assert [event[0] for event in held.manipulation.events].count("close") == 1

    dropped = _skill(closes_empty=False, empties_on_lift=True)
    dropped._close_twist_lift("brick", 0.25, 0.0, 0.0, 1.3, 0.0)
    assert [event[0] for event in dropped.manipulation.events].count("open") == 2
    assert [event[0] for event in dropped.manipulation.events].count("close") == 3
    assert [event[0] for event in dropped.manipulation.events].count("lift") == 3

    approach = _Approach()
    held.sleep = lambda _seconds: None
    assert held._grasp_verified("brick", approach)
    assert approach.moves == []


def test_model_grasp_preserves_raised_rigid_hold_without_reseeding_grip(monkeypatch):
    monkeypatch.setattr("innate_skills.pick_any_object.time.sleep", lambda _seconds: None)
    skill = _skill(closes_empty=False)
    skill._pickup_policy = object()
    skill._unpress_grasp = False
    skill._close_twist_lift("brick", 0.25, 0, 0, 1.3, 0)
    assert skill._holding
    assert not any(e[0] == "move_to" for e in skill.manipulation.events)
    before = list(skill.manipulation.events)
    skill._rest_arm(keep_grip=True)
    assert skill.manipulation.events == before
    # A low arm still needs safe teardown; retaining it would drag the object.
    skill.manipulation.pose.z = 0.04
    skill._rest_arm(keep_grip=True)
    assert len(skill.manipulation.events) == len(before) + 2


def test_model_descent_keeps_small_steps_fresh_frames_and_cancel_points():
    import pytest

    def make(cancel_after=None):
        skill = _skill(closes_empty=False)
        skill.manipulation.pose = SimpleNamespace(z=0.15, position=(0.3, 0, 0.15))
        moves, frames = [], []

        def move(x, y, z, **kwargs):
            moves.append((z, kwargs["duration"]))
            skill.manipulation.pose = SimpleNamespace(z=z, position=(x, y, z))

        skill.manipulation.move_to = move
        skill.manipulation.clamp_reach = lambda x, y: (x, y)
        skill.wrist_image = object()

        def frame(_previous):
            frames.append(True)
            return True, object()

        skill._next_wrist_hsv = frame
        skill.sleep = lambda _seconds: None

        def check():
            if cancel_after and len(moves) >= cancel_after:
                raise InterruptedError("Stop")

        skill.check_cancelled = check
        decisions = iter(
            [
                dict(
                    action="descend",
                    center=[320, skill._p["wrist_box_v"]],
                    delta=[0, 0, -0.10],
                    roll=0,
                    note="centered",
                ),
                dict(action="grasp", center=[320, skill._p["wrist_box_v"]], delta=[0, 0, 0], roll=0, note="aligned"),
            ]
        )
        skill._pickup_policy = SimpleNamespace(decide=lambda *_args, **_kwargs: next(decisions))
        return skill, moves, frames

    skill, moves, frames = make()
    assert skill._wrist_descend_model("brick") == pytest.approx((0.3, 0, 0.05, 0))
    assert len(moves) == 10 and all(duration == 0.5 for _z, duration in moves)
    assert len(frames) >= len(moves) + 2
    assert not skill._unpress_grasp
    cancelled, moves, frames = make(cancel_after=3)
    with pytest.raises(InterruptedError):
        cancelled._wrist_descend_model("brick")
    assert len(moves) == 3


def test_pickup_reports_a_drop_during_the_final_carry_motion(monkeypatch):
    approach = SimpleNamespace(search=lambda _: (0.3, 0), position_above=lambda _, xy: xy)
    monkeypatch.setattr("innate_skills.pick_any_object.FloorApproach", lambda *_: approach)
    for dropped in (False, True):
        skill = _skill(closes_empty=False)
        events = []
        skill._proxy = object()
        skill.head = SimpleNamespace(set_position=lambda _: None)
        skill.mobility = SimpleNamespace(stop=lambda: None)
        skill.say = events.append
        skill.fail = lambda message: (_ for _ in ()).throw(exceptions.SkillFailed(message))
        skill._detect_px = lambda *_: None
        skill._grasp_at = lambda *_: None
        skill._grasp_verified = lambda *_: True

        def carry(skill=skill, events=events, dropped=dropped, **_):
            events.append("carry finished")
            skill.joint_states.position[5] = -0.085 if dropped else 0.12

        skill._rest_arm = carry
        if dropped:
            with pytest.raises(exceptions.SkillFailed, match="slipped"):
                skill.execute("brick", controller="classic")
            assert "Got it." not in events
        else:
            assert "carry motion" in skill.execute("brick", controller="classic")
            assert events.index("Got it.") > events.index("carry finished")
