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
for resource_type in (
    "Head",
    "HeadState",
    "JointStates",
    "MainImage",
    "Manipulation",
    "Mobility",
    "Odometry",
    "WristImage",
):
    setattr(innate, resource_type, type(resource_type, (), {}))


class _Waypoint:
    def __init__(self, _x, _y, z, **_kwargs):
        self.z = z


innate.Waypoint = _Waypoint
innate.resource = lambda function: function
innate.gemini = ModuleType("innate.gemini")
innate.vision = ModuleType("innate.vision")
innate.vision.Axis = type("Axis", (), {})
innate.vision.IMG_W, innate.vision.IMG_H = 640, 480
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
    skill._pickup_policy = None
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


@pytest.mark.parametrize("policy", [None, object()])
def test_grasp_preserves_raised_rigid_hold_without_reseeding_grip(monkeypatch, policy):
    monkeypatch.setattr("innate_skills.pick_any_object.time.sleep", lambda _seconds: None)
    skill = _skill(closes_empty=False)
    skill._pickup_policy = policy
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


def test_pending_stop_at_arrival_prevents_any_grasp_motion():
    skill = _skill(closes_empty=False)
    skill.check_cancelled = lambda: (_ for _ in ()).throw(InterruptedError("Stop"))
    with pytest.raises(InterruptedError, match="Stop"):
        skill._grasp_at("brick", (0.3, 0))
    assert skill.manipulation.events == []


def test_rigid_floor_close_is_shared_while_soft_and_unknown_keep_unpress():
    for controller in (None, object()):
        for strength in (0.35, 0.6, None):
            skill = _skill(closes_empty=False)
            skill._pickup_policy = controller
            skill._grip_strength = strength
            skill.manipulation.pose.z = 0.03
            skill._pre_close_lift(0.3, 0, 0, 1.3, 0)
            if strength == 0.35:
                assert skill.manipulation.events == []
                assert skill.manipulation.pose.z == 0.03
            else:
                assert skill.manipulation.events == [("move_to", 0.04)]


def test_planned_descent_keeps_small_steps_final_confirmation_and_cancel_points(monkeypatch):
    monkeypatch.setattr("innate_skills.pick_any_object.vision.b64_to_hsv", lambda x: x, raising=False)
    monkeypatch.setattr("innate_skills.pick_any_object.inside_box", lambda *_: True)

    def make(cancel_after=None):
        skill = _skill(closes_empty=False)
        skill._pickup_policy = object()
        skill._planned_roll = -1.5
        skill.manipulation.pose = SimpleNamespace(z=0.15, position=(0.3, 0, 0.15))
        moves, frames = [], []
        center = (320, skill._p["wrist_box_v"])
        skill._wrist_seed = lambda _: (center, (300, 300, 40, 40))
        from contextlib import nullcontext

        tracker = SimpleNamespace(
            ok=True, axis=None, update=lambda _: center, during_motion=lambda *_: nullcontext({"raw": None})
        )
        skill._new_wrist_tracker = lambda *_: (tracker, None)

        def move(x, y, z, **kwargs):
            moves.append((z, kwargs["duration"]))
            skill.manipulation.pose = SimpleNamespace(z=z, position=(x, y, z))

        skill.manipulation.move_to = move
        skill.manipulation.clamp_reach = lambda x, y: (x, y)

        def frame(_):
            frames.append(True)
            return True, object()

        skill._next_wrist_hsv = frame

        def check():
            if cancel_after and len(moves) >= cancel_after:
                raise InterruptedError("Stop")

        skill.check_cancelled = check
        return skill, moves, frames

    skill, moves, frames = make()
    assert PickAnyObject._wrist_descend(skill, "brick", 0.3, 0) == pytest.approx((0.3, 0, 0.05, -1.5))
    assert len(moves) == 10 and all(duration == 0.5 for _, duration in moves)
    assert len(frames) >= len(moves) + 2
    cancelled, moves, _ = make(cancel_after=3)
    with pytest.raises(InterruptedError):
        PickAnyObject._wrist_descend(cancelled, "brick", 0.3, 0)
    assert len(moves) == 3


def test_lower_search_never_increases_any_joint_travel_at_the_same_duration():
    from innate_skills.pick_any_object import NAV_ARM, WRIST_SEARCH_ARM

    cases = (
        (NAV_ARM, "flat", 0.35, True),
        (NAV_ARM, "low", 0.35, True),
        (NAV_ARM, "high", 0.35, False),
        (NAV_ARM, "flat", 0.6, False),
        (NAV_ARM, "flat", None, False),
        ([0, 0.2, 0.1, 1.0, -0.0491], "flat", 0.35, False),
    )
    for start, clearance, strength, lowered in cases:
        skill = _skill(closes_empty=False)
        skill._pickup_policy = object()
        skill.joint_states.position = [*start, skill.manipulation.GRIPPER_OPEN]
        skill._search_clearance = clearance
        skill._grip_strength = strength
        skill.sleep = lambda _: None
        commands = []
        skill.manipulation.move_joints = lambda joints, duration, commands=commands: commands.append((joints, duration))
        PickAnyObject._goto_search_pose(skill, 0)
        target, duration = commands[0]
        old = [
            0,
            WRIST_SEARCH_ARM[1],
            WRIST_SEARCH_ARM[2],
            skill._p["wrist_pitch"] - sum(WRIST_SEARCH_ARM[1:3]),
            WRIST_SEARCH_ARM[4],
        ]
        assert duration == skill._p["hover_s"]
        assert all(abs(n - s) <= abs(o - s) + 1e-6 for n, o, s in zip(target[:5], old, start, strict=True))
        if lowered:
            assert target[2] > 0  # selected the lower search instead of original
        else:
            assert target[1:5] == pytest.approx(old[1:5])
    assert target[:5] == pytest.approx(old)  # unfavorable start falls back to original


def test_search_open_keeps_verified_sdk_recovery_for_shut_or_missing_telemetry():
    for aperture in (0.8, -0.085, float("nan"), None):
        skill = _skill(closes_empty=False)
        skill._pickup_policy = object()
        skill.joint_states = None if aperture is None else SimpleNamespace(position=[0] * 5 + [aperture])
        skill._prepare_wrist_search(0)
        events = skill.manipulation.events
        assert events[0] == ("search",)
        assert [event for event in events if event[0] == "open"] == ([] if aperture == 0.8 else [("open", 1.0)])
    skill = _skill(closes_empty=False)
    skill._pickup_policy = object()
    skill.joint_states.position[5] = 0.8

    def search(_bearing):
        skill.manipulation.events.append(("search",))
        if len(skill.manipulation.events) == 1:
            raise exceptions.ArmFailed("tripped claw rejected combined move")

    skill._goto_search_pose = search
    skill._prepare_wrist_search(0)
    assert skill.manipulation.events == [("search",), ("open", 1.0), ("search",)]


def test_wrist_plan_uses_a_fresh_frame_and_refuses_a_frozen_camera(monkeypatch):
    skill = _skill(closes_empty=False)
    skill._pickup_policy = object()
    skill.wrist_image = object()
    fresh = object()
    seen = []
    skill._next_wrist_hsv = lambda old, **_: (True, fresh) if old is skill.wrist_image else (None, old)
    plan = {"box_2d": [100, 200, 300, 400], "axis_2d": [], "grasp_point_2d": [200, 300]}
    skill._observe_pickup = lambda _prompt, image, _view: seen.append(image) or {"detections": [plan]}
    monkeypatch.setattr(
        "innate_skills.pick_any_object.vision.parse_det_boxes", lambda _: [(300, 280, 40, 40)], raising=False
    )
    skill._wrist_seed("brick")
    assert seen == [fresh]
    skill._next_wrist_hsv = lambda old, **_: (None, old)
    with pytest.raises(exceptions.SkillFailed, match="fresh wrist image"):
        skill._wrist_seed("brick")
    assert seen == [fresh]  # never infer from the stale frame


def test_head_perception_finishes_fold_before_returning_a_target(monkeypatch):
    skill = _skill(closes_empty=False)
    events = []
    skill._pickup_policy = object()
    skill._nav_pending = True
    skill.main_image = "image"
    skill._settled_head_image = lambda: events.append("settle") or skill.main_image
    skill.mobility = SimpleNamespace(stop=lambda: events.append("stop"))
    skill.sleep = lambda _: events.append("settle")
    skill._observe_pickup = lambda *_: events.append("perception") or {"detections": []}
    skill.manipulation.wait = lambda: events.append("join")
    monkeypatch.setattr("innate_skills.pick_any_object.vision.parse_det_cands", lambda _: [], raising=False)
    assert skill._detect_px("brick") is None
    assert events == ["stop", "settle", "perception", "join"]
    assert not skill._nav_pending


def test_pickup_reports_a_drop_during_the_final_carry_motion(monkeypatch):
    approach = SimpleNamespace(search=lambda _: (0.3, 0), position_above=lambda _, xy: xy)

    def make_approach(*_, **kwargs):
        assert kwargs["confirm_arrival"] is True
        return approach

    monkeypatch.setattr("innate_skills.pick_any_object.FloorApproach", make_approach)
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


@pytest.mark.parametrize("mode", ("healthy", "missing", "stale", "moving", "bad_angle", "frozen_camera"))
def test_head_settling_requires_fresh_stationary_feedback_and_a_later_image(monkeypatch, mode):
    skill = _skill(closes_empty=False)
    skill._p.update(settle_s=1.2, tilt_deg=-20)
    now = [0.0]
    skill.main_image = object()

    def update():
        skill.head_position = (
            SimpleNamespace(pitch_degrees=-15 if mode == "bad_angle" else -20, raw_source=object())
            if mode != "missing"
            else None
        )
        skill.odom = SimpleNamespace(
            linear_velocity=0.03 if mode == "moving" else 0, angular_velocity=0, raw_source=object()
        )

    update()

    def sleep(seconds):
        now[0] += seconds
        if mode != "stale":
            update()
        if mode != "frozen_camera":
            skill.main_image = object()

    skill.sleep = sleep
    monkeypatch.setattr("innate_skills.pick_any_object.time.monotonic", lambda: now[0])
    assert skill._settled_head_image() is skill.main_image
    if mode == "healthy":
        assert 0.15 < now[0] < 0.5
    else:
        assert now[0] >= 1.2


@pytest.mark.parametrize(
    ("confirm", "flow_result", "projectable", "head_looks"),
    ((True, "in_box", True, 1), (False, "in_box", True, 0), (False, "in_box", False, 1), (False, "timeout", True, 1)),
)
def test_flow_arrival_skips_head_only_for_confirmed_projectable_arrival(
    monkeypatch, confirm, flow_result, projectable, head_looks
):
    import importlib.util

    repo = Path(__file__).resolve().parents[1]

    def load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    real_geometry = load("pickup_test_geometry", repo / "ros2_ws/src/brain/brain_client/innate/geometry.py")
    monkeypatch.setitem(sys.modules, "innate.geometry", real_geometry)
    module = load("pickup_test_approach", repo / "workspace/innate_skills/approach.py")
    monkeypatch.setattr(module, "floor_to_pixel", lambda *_: (320, 200))
    monkeypatch.setattr(module, "pixel_to_floor", lambda *_: (0.28, 0) if projectable else None)
    host = SimpleNamespace(main_image="frame", mobility=SimpleNamespace(stop=lambda: None))
    approach = module.FloorApproach(host, dict(module.APPROACH_PARAMS), None, confirm_arrival=confirm)
    approach.odom_xyt = lambda: (0, 0, 0)
    approach._sweet_box = lambda: ((320, 300), (40, 40), (20, 20))
    approach._follow_into_box = lambda *_args, **_kwargs: (flow_result, (320, 300))
    looks = []
    approach._localize_retry = lambda _: looks.append("head") or ((0.28, 0), (320, 300))
    assert approach.position_above("the brick", (0.4, 0)) == (0.28, 0)
    assert len(looks) == head_looks


@pytest.mark.parametrize(
    "axis,roll",
    [
        ([], 0),
        ([200, 200, 400, 350], -0.7853981633974483),
        ([400, 350, 200, 200], -0.7853981633974483),
        ([200, 200, 200, 400], -1.5),
        ([200, 300, 400, 300], 0),
    ],
)
def test_wrist_plan_keeps_head_material_and_converts_image_axis(monkeypatch, axis, roll):
    skill = _skill(closes_empty=False)
    skill._pickup_policy = object()
    skill._grip_strength = 0.6
    skill.wrist_image = "old"
    skill._next_wrist_hsv = lambda *_args, **_kwargs: (object(), "new")
    skill._observe_pickup = lambda *_: {
        "detections": [{"box_2d": [100, 100, 500, 500], "axis_2d": axis, "grasp_point_2d": [300, 300]}]
    }
    monkeypatch.setattr(
        "innate_skills.pick_any_object.vision.parse_det_boxes", lambda _: [(300, 300, 40, 40)], raising=False
    )
    assert skill._wrist_seed("sock") == ((192, 144), (300, 300, 40, 40))
    assert skill._planned_roll == pytest.approx(roll)
    assert skill._grip_strength == 0.6


@pytest.mark.parametrize("policy", [None, object()])
@pytest.mark.parametrize("recovery", ["returns", "offline", "cancel", "target_missing"])
def test_camera_disconnect_holds_then_requires_new_grasp_confirmations(monkeypatch, recovery, policy):
    from contextlib import nullcontext

    monkeypatch.setattr("innate_skills.pick_any_object.vision.b64_to_hsv", lambda x: x, raising=False)
    monkeypatch.setattr("innate_skills.pick_any_object.inside_box", lambda *_: True)
    skill = _skill(closes_empty=False)
    skill._pickup_policy = policy
    skill._planned_roll = 0
    skill.manipulation.pose = SimpleNamespace(position=(0.3, 0, 0.06))
    skill._wrist_seed = lambda _: ((320, 350), (280, 300, 80, 100))
    events = []
    old = SimpleNamespace(
        ok=True,
        axis=None,
        update=lambda _: events.append("old_track") or (320, 350),
        during_motion=lambda *_: nullcontext({"raw": "last", "gap": True}),
    )
    fresh = SimpleNamespace(ok=True, axis=None, update=lambda _: events.append("new_track") or (320, 350))
    skill._new_wrist_tracker = lambda *_: (old, "seed")
    skill.manipulation.move_to = lambda *_args, **_kw: events.append("move")
    calls = 0

    def frame(raw, timeout=1.5):
        nonlocal calls
        calls += 1
        if calls == 3:
            events.append("outage")
            return None, raw
        if calls == 4:
            assert timeout <= 5
            assert events.count("move") == 1
            events.append("reconnect_wait")
            if recovery == "cancel":
                raise InterruptedError("Stop")
            if recovery == "offline":
                return None, raw
        return True, object()

    skill._next_wrist_hsv = frame

    def reseed(_prompt, raw, frame=None):
        assert frame == (True, raw)
        events.append("reseed")
        if recovery == "target_missing":
            return None, raw, "lost track"
        return fresh, raw, ""

    skill._wrist_reseed = reseed
    if recovery == "returns":
        assert PickAnyObject._wrist_descend(skill, "sock", 0.3, 0) == pytest.approx((0.3, 0, 0.05, 0))
        assert events == [
            "old_track",
            "old_track",
            "move",
            "outage",
            "reconnect_wait",
            "reseed",
            "new_track",
            "new_track",
        ]
    else:
        with pytest.raises(InterruptedError if recovery == "cancel" else exceptions.SkillFailed):
            PickAnyObject._wrist_descend(skill, "sock", 0.3, 0)
        assert events.count("move") == 1
        assert "new_track" not in events


def test_repeated_camera_interruptions_exhaust_budget_without_moving():
    skill = _skill(closes_empty=False)
    skill._pickup_policy = object()
    skill._planned_roll = 0
    skill.manipulation.pose = SimpleNamespace(position=(0.3, 0, 0.1))
    skill._wrist_seed = lambda _: ((320, 350), (280, 300, 80, 100))
    tracker = SimpleNamespace(ok=True)
    skill._new_wrist_tracker = lambda *_: (tracker, "seed")
    skill._next_wrist_hsv = lambda raw, timeout=1.5: (None, raw) if timeout == 1.5 else (True, object())
    reacquisitions = []

    def reseed(*_args, **_kwargs):
        reacquisitions.append(True)
        return tracker, object(), ""

    skill._wrist_reseed = reseed
    with pytest.raises(exceptions.SkillFailed, match="camera recovery budget exhausted"):
        PickAnyObject._wrist_descend(skill, "sock", 0.3, 0)
    assert len(reacquisitions) == 2
    assert skill.manipulation.events == []


@pytest.mark.parametrize("camera_online", [True, False])
def test_stationary_reseed_latency_is_not_camera_silence(monkeypatch, camera_online):
    clock = [0.0]
    monkeypatch.setattr("innate_skills.pick_any_object.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("innate_skills.pick_any_object.inside_box", lambda *_: True)
    skill = _skill(closes_empty=False)
    skill._pickup_policy = object()
    skill._planned_roll = 0
    skill.manipulation.pose = SimpleNamespace(position=(0.3, 0, 0.05))
    skill._wrist_seed = lambda _: ((320, 350), (280, 300, 80, 100))
    events = []
    lost = SimpleNamespace(ok=True, misses=3, axis=None, update=lambda _: None)
    fresh = SimpleNamespace(ok=True, axis=None, update=lambda _: events.append("track") or (320, 350))
    skill._new_wrist_tracker = lambda *_: (lost, "initial")

    def frame(raw, timeout=1.5):
        clock[0] += 0.1
        if raw == "reseed" and not camera_online:
            clock[0] += timeout
            return None, raw
        return True, object()

    def reseed(_prompt, _raw, frame=None):
        # The arm is stationary while inference runs. Its original seed frame
        # remains the reference; confirmations must come from later frames.
        assert frame is None, "Healthy camera must not trigger a second recovery"
        assert not events
        events.append("reseed")
        clock[0] += 3.0
        return fresh, "reseed", ""

    skill._next_wrist_hsv = frame
    skill._wrist_reseed = reseed
    if camera_online:
        assert PickAnyObject._wrist_descend(skill, "sock", 0.3, 0) == pytest.approx((0.3, 0, 0.05, 0))
        assert events == ["reseed", "track", "track"]
    else:
        with pytest.raises(exceptions.SkillFailed, match="wrist camera did not recover"):
            PickAnyObject._wrist_descend(skill, "sock", 0.3, 0)
        assert events == ["reseed"]
    assert skill.manipulation.events == []


@pytest.mark.parametrize("strength,rigid", [(0.35, True), (0.5, False), (0.0, False)])
def test_wrist_tracker_uses_model_seed_and_known_material(monkeypatch, strength, rigid):
    skill = _skill(closes_empty=False)
    skill._pickup_policy = object()
    skill._grip_strength = strength
    frame, raw, tracker = object(), object(), object()
    skill._wrist_seed_frame = (frame, raw)

    def make(image, box, point, **options):
        assert image is frame
        assert (box, point) == ((10, 20, 30, 40), (25, 40))
        assert options == {"rigid": rigid}
        return tracker

    monkeypatch.setattr("innate_skills.grasp_tracker.make_grasp_tracker", make)
    assert skill._new_wrist_tracker((10, 20, 30, 40), (25, 40), None) == (tracker, raw)


@pytest.mark.parametrize("cancel", [False, True])
def test_open_abort_execute_finally_preserves_posture_and_original_failure(monkeypatch, cancel):
    class Cancelled(BaseException):
        pass

    failure = Cancelled("stop") if cancel else exceptions.SkillFailed("wrist veto")
    skill = _skill(closes_empty=False)
    commands = []
    skill._proxy = object()
    skill.head = SimpleNamespace(set_position=lambda _: None)
    skill.mobility = SimpleNamespace(stop=lambda: commands.append("base stop"))
    skill.manipulation.move_joints = lambda *a, **k: commands.append("initial navigation fold")
    skill.manipulation.wait = lambda: None
    skill.say = lambda _: None
    skill.sleep = lambda _: None
    skill.fail = lambda text: (_ for _ in ()).throw(exceptions.SkillFailed(text))
    approach = SimpleNamespace(search=lambda _: (0.3, 0), position_above=lambda _, xy: xy)
    monkeypatch.setattr("innate_skills.pick_any_object.FloorApproach", lambda *a, **k: approach)

    skill.manipulation.torque_on = lambda: None
    skill.manipulation.clamp_reach = lambda x, y: (x, y)
    skill._prepare_wrist_search = lambda _: commands.append("open search")

    def veto(*a):
        assert skill._open_pregrasp is True
        # Any cleanup command would be a regression; the real _rest_arm is used.
        skill.manipulation.move_joints = lambda *a, **k: commands.append("unexpected fold")
        skill.manipulation.move_to = lambda *a, **k: commands.append("unexpected retreat")
        skill.manipulation.gripper_close = lambda *a, **k: commands.append("unexpected close")
        skill.manipulation.torque_off = lambda *a, **k: commands.append("unexpected torque off")
        raise failure

    skill._wrist_descend = veto
    with pytest.raises(type(failure)) as caught:
        skill.execute("cube")
    assert caught.value is failure
    assert commands == ["initial navigation fold", "open search", "base stop"]


@pytest.mark.parametrize("reason", ["not seen", "no wrist frames", "lost track", "timeout", "reach limit"])
def test_classic_tracking_failure_cannot_continue_to_blind_grasp(reason):
    skill = _skill(closes_empty=False)
    with pytest.raises(exceptions.SkillFailed, match=reason):
        skill._wrist_done(0.3, 0, 0.08, reason)
    assert skill.manipulation.events == []


def test_classic_camera_generation_change_reacquires_before_tracking():
    skill = _skill(closes_empty=False)
    skill.manipulation.pose = SimpleNamespace(position=(0.3, 0, 0.08))
    skill._wrist_seed = lambda _: ((320, 350), (280, 300, 80, 100))
    stale = SimpleNamespace(capture_is_current=lambda: False)
    tracker = SimpleNamespace(ok=True)
    skill._new_wrist_tracker = lambda *_: (tracker, stale)
    fresh = object()
    skill._next_wrist_hsv = lambda *_: (True, fresh)
    calls = []

    def reseed(_prompt, raw, frame=None):
        assert raw is fresh and frame == (True, fresh)
        calls.append("reseed")
        return None, raw, "target missing after reconnect"

    skill._wrist_reseed = reseed
    with pytest.raises(exceptions.SkillFailed, match="target missing after reconnect"):
        PickAnyObject._wrist_descend(skill, "brick", 0.3, 0)
    assert calls == ["reseed"]
    assert skill.manipulation.events == []
