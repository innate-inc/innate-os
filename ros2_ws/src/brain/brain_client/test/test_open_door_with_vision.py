# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import importlib
import json
import logging
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("rclpy")

from brain_client.skills import debug_runs
from brain_client.skills.types import SkillFailed

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


def test_localizes_handle_from_cabinet_floor_edge():
    # Synthetic projection of a handle at x=0.8 m, z=0.2 m and two points
    # spanning the same cabinet/floor edge, using the calibrated head camera.
    point, left, right, plane_yaw = geometry.handle_from_floor_edge(
        (319.1, 268.3231),
        (226.0430, 335.3592),
        (411.9058, 335.3592),
        0.0,
    )

    # The optical center ray starts at the camera's +29.5 mm base_link Y
    # offset; center pixel therefore intersects the plane on that same line.
    assert point == pytest.approx((0.8, 0.0295, 0.2), abs=0.003)
    assert left[0] == pytest.approx(0.8, abs=0.003)
    assert right[0] == pytest.approx(0.8, abs=0.003)
    assert abs(abs(plane_yaw) - math.pi / 2.0) < 0.01


def test_known_vertical_height_projects_to_metric_target():
    point, optical_range = geometry.vertical_handle_target(
        (335.0, 228.0, 10.0, 64.0),
        0.10,
        fx=195.36129809912026,
        fy=259.5741983485189,
        cx=317.75570636221465,
        cy=228.0517433641685,
        camera_origin=(0.002519, 0.0295, 0.258545),
    )

    assert optical_range == pytest.approx(0.4055847)
    assert point == pytest.approx((0.40810, -0.01670, 0.20855), abs=0.0001)


def test_wrist_camera_recovers_known_vertical_handle_with_mount_pitch():
    focal = 480.0 / (2.0 * math.tan(math.radians(80.0) / 2.0))
    camera_origin, camera_rotation = geometry.camera_pose_from_ee(
        (0.32, -0.01, 0.21),
        (0.0, 0.0, 0.0),
        translation_in_ee=(-0.058058, 0.0, 0.05052),
        rpy_in_ee=(0.0, 0.43633, 0.0),
    )
    # Synthetic projection of a 100 mm vertical handle centered at this point.
    expected = (0.428, -0.01, 0.207)
    inverse = tuple(zip(*camera_rotation, strict=True))

    def project(point):
        relative = tuple(point[index] - camera_origin[index] for index in range(3))
        forward, left, up = (sum(inverse[row][column] * relative[column] for column in range(3)) for row in range(3))
        return 320.0 - focal * left / forward, 240.0 - focal * up / forward

    top_u, top_v = project((expected[0], expected[1], expected[2] + 0.05))
    bottom_u, bottom_v = project((expected[0], expected[1], expected[2] - 0.05))
    box = (top_u - 5.0, top_v, 10.0, bottom_v - top_v)

    point, residual, top_range, bottom_range = geometry.vertical_handle_from_camera_box(
        box,
        0.10,
        fx=focal,
        fy=focal,
        cx=320.0,
        cy=240.0,
        camera_origin=camera_origin,
        camera_rotation=camera_rotation,
    )

    assert point == pytest.approx(expected, abs=1e-6)
    assert residual == pytest.approx(0.0, abs=1e-8)
    assert top_range > 0.0
    assert bottom_range > 0.0


def test_metric_box_rejects_clipped_handle():
    with pytest.raises(ValueError, match="full top and bottom"):
        geometry.validate_vertical_box((330.0, 2.0, 12.0, 100.0))


def test_metric_box_rejects_unstable_height():
    with pytest.raises(ValueError, match="size estimate is unstable"):
        geometry.stable_vertical_box(
            [
                (330.0, 200.0, 12.0, 80.0),
                (330.0, 190.0, 12.0, 100.0),
                (330.0, 205.0, 12.0, 70.0),
            ]
        )


def test_handle_floor_anchor_projects_under_handle_on_sloped_edge():
    response = '[{"box_2d":[250,450,650,550],"floor_left":[800,100],"floor_right":[700,900]}]'

    floor, box = module._handle_floor_anchor(response)

    assert box == (288, 120, 64, 192)
    assert floor == pytest.approx((320.0, 360.0))


def test_handle_floor_anchor_keeps_box_and_floor_points_from_same_detection():
    response = (
        '[{"box_2d":[250,450,650,550]},{"box_2d":[250,450,650,550],"floor_left":[800,100],"floor_right":[700,900]}]'
    )

    floor, _box = module._handle_floor_anchor(response)

    assert floor == pytest.approx((320.0, 360.0))


@pytest.mark.parametrize(
    "response",
    [
        "[]",
        '[{"box_2d":[250,450,650,550]}]',
        '[{"box_2d":[250,450,650,550],"floor_left":[800,480],"floor_right":[800,520]}]',
        '[{"box_2d":[700,450,900,550],"floor_left":[600,100],"floor_right":[600,900]}]',
    ],
)
def test_handle_floor_anchor_rejects_missing_or_inconsistent_geometry(response):
    assert module._handle_floor_anchor(response) is None


def test_detection_exports_exact_camera_frame(tmp_path, monkeypatch):
    request = {}

    def ask_image(*_args, **kwargs):
        request.update(kwargs)
        return '[{"box_2d":[200,450,800,550],"grasp_point":[500,500]}]'

    monkeypatch.setattr(debug_runs, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(module.gemlib, "ask_image", ask_image)
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-frame-test"))
    skill._configure_debug_run(run_id="frame-run", skill_id="innate-os/open_door_with_vision", inputs={})
    image = type("Frame", (), {"jpeg": b"exact-jpeg"})()

    assert skill._detect_box(image, "head") == pytest.approx((288, 96, 64, 288))
    assert request["reasoning_effort"] == "minimal"
    assert request["model"] == "gemini-3.6-flash"
    assert (skill.debug_directory / "00_head_detection.jpg").read_bytes() == b"exact-jpeg"


def test_floor_detector_uses_fast_vlm_and_returns_point_under_handle(monkeypatch):
    request = {}

    def ask_image(*_args, **kwargs):
        request.update(kwargs)
        return '[{"box_2d":[250,450,650,550],"floor_left":[800,100],"floor_right":[700,900]}]'

    monkeypatch.setattr(module.gemlib, "ask_image", ask_image)
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-floor-detection-test"))
    skill.mobility = _Mobility()
    skill.main_image = type("Frame", (), {"jpeg": b"exact-jpeg"})()
    monkeypatch.setattr(skill, "sleep", lambda _seconds: None)

    point = skill._detect_handle_floor_px("the yellow handle")

    assert point == pytest.approx((320.0, 360.0))
    assert request["reasoning_effort"] == "minimal"
    assert request["model"] == "gemini-3.6-flash"
    assert skill.mobility.stops == 1


def test_detection_exports_raw_response_before_parse_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(debug_runs, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(module.gemlib, "ask_image", lambda *_args, **_kwargs: "not-json")
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-response-test"))
    skill._configure_debug_run(run_id="response-run", skill_id="innate-os/open_door_with_vision", inputs={})
    image = type("Frame", (), {"jpeg": b"exact-jpeg"})()

    with pytest.raises(SkillFailed, match="Could not identify"):
        skill._detect_box(image, "wrist")

    events = [json.loads(line) for line in (skill.debug_directory / "events.jsonl").read_text().splitlines()]
    response = next(event for event in events if event["event"] == "handle_detection_response")
    parsed = next(event for event in events if event["event"] == "handle_detection_parse")
    assert response["camera"] == "wrist"
    assert response["response"] == "not-json"
    assert response["frame"] == "00_wrist_detection.jpg"
    assert parsed["boxes"] == []


def test_wrist_decision_retries_malformed_content_and_logs_raw_responses(tmp_path, monkeypatch):
    responses = iter(
        [
            "not-json",
            '[{"box_2d":[200,450,800,550],"grasp_point":[500,500],"action":"RIGHT","reason":"align"}]',
        ]
    )
    requests = []

    def ask_image(_proxy, images, prompt, **kwargs):
        requests.append((images, prompt, kwargs))
        return next(responses)

    monkeypatch.setattr(debug_runs, "get_workspace_dir", lambda: tmp_path)
    monkeypatch.setattr(module.gemlib, "ask_image", ask_image)
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-action-response-test"))
    skill._configure_debug_run(run_id="action-response-run", skill_id="innate-os/open_door_with_vision", inputs={})
    previous = type("Frame", (), {"jpeg": b"previous"})()
    current = type("Frame", (), {"jpeg": b"current"})()

    action, box, reason = skill._request_wrist_decision(
        current,
        previous_image=previous,
        previous_action="FORWARD",
        step=3,
    )

    assert (action, box, reason) == ("RIGHT", (288.0, 96.0, 64.0, 288.0), "align")
    assert len(requests) == 2
    assert requests[0][0] == [previous, current]
    assert "Image 2 is the current frame after action FORWARD" in requests[0][1]
    assert "UP and DOWN describe the physical GRIPPER motion in the robot base frame" in requests[0][1]
    assert "choose UP only when the gripper/end effector itself should rise to a higher Z" in requests[0][1]
    assert "raising the gripper normally makes the stationary handle appear LOWER" in requests[0][1]
    assert "Never choose UP or DOWN based on the direction you want the handle to move" in requests[0][1]
    assert "opposite sides of the handle SHAFT" in requests[0][1]
    assert "Do NOT choose GRASP when only the rounded free end or tip" in requests[0][1]
    assert "move the gripper UP when needed to surround a higher shaft section" in requests[0][1]
    assert "TOP END of the vertical handle is just visible" in requests[0][1]
    assert "assume the gripper is too low and choose UP" in requests[0][1]
    assert "Do not ABORT merely because the gripper temporarily occludes the handle" in requests[0][1]
    assert requests[0][2]["reasoning_effort"] == "minimal"
    assert requests[0][2]["model"] == "gemini-3.6-flash"
    assert (skill.debug_directory / "00_wrist_action_3.jpg").read_bytes() == b"current"
    events = [json.loads(line) for line in (skill.debug_directory / "events.jsonl").read_text().splitlines()]
    raw = [event for event in events if event["event"] == "wrist_action_response"]
    assert [event["response"] for event in raw] == [
        "not-json",
        '[{"box_2d":[200,450,800,550],"grasp_point":[500,500],"action":"RIGHT","reason":"align"}]',
    ]
    assert raw[0]["content_attempt"] == 1
    assert raw[1]["content_attempt"] == 2


def test_gemini_image_request_forwards_reasoning_effort():
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def read(self):
            return b'{"choices":[{"message":{"content":"[]"}}]}'

    class Stream:
        def __enter__(self):
            return Response()

        def __exit__(self, *_args):
            return False

    class Client:
        def request_stream(self, _service, _endpoint, **kwargs):
            captured.update(kwargs["json"])
            return Stream()

    assert (
        module.gemlib.ask_image(
            Client(),
            "jpeg",
            "find it",
            retries=1,
            reasoning_effort="minimal",
            model="gemini-3.6-flash",
        )
        == "[]"
    )
    assert captured["reasoning_effort"] == "minimal"
    assert captured["model"] == "gemini-3.6-flash"


def test_floor_approach_targets_arm_axis_at_grasp_standoff():
    assert module._DOOR_APPROACH_PARAMS["sweet_x"] == pytest.approx(0.40)
    assert module._DOOR_APPROACH_PARAMS["box_y"] == pytest.approx(-0.05285)
    assert module._DOOR_APPROACH_PARAMS["tilt_deg"] == pytest.approx(-20.0)


def test_tracked_plane_overrides_wrong_known_size_range(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-plane-range-test"))
    skill._handle_height_m = 0.10
    monkeypatch.setattr(skill, "sleep", lambda _seconds: None)
    # Reproduces the live MARS 47 disagreement: the complete visible handle is
    # 104 px tall, which makes a 100 mm size estimate say 0.25 m, while the
    # odometry-pinned floor approach tracked the cabinet plane at 0.459 m.
    monkeypatch.setattr(
        skill,
        "_measure_handle",
        lambda _camera, _fy: ((358.0, 228.5, 17.0, 104.0), 0.2496),
    )

    target = skill._localize_handle((0.45915, -0.10446))

    assert target[0] == pytest.approx(0.45915)
    assert target[1] == pytest.approx(-0.0786, abs=0.002)
    assert target[2] == pytest.approx(0.2042, abs=0.002)


def test_shared_floor_approach_is_used_for_handle_parking(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-floor-approach-test"))
    calls = []

    class Approach:
        def __init__(self, host, params, detect, debug):
            calls.append(("init", host, params, detect, debug))

        def search(self, prompt):
            calls.append(("search", prompt))
            return (0.8, 0.1)

        def position_above(self, prompt, xy):
            calls.append(("position", prompt, xy))
            return (0.40, -0.05285)

    debug = object()
    monkeypatch.setattr(module, "Debug", lambda _host, _params: debug)
    monkeypatch.setattr(module, "FloorApproach", Approach)
    monkeypatch.setattr(skill, "say", lambda text, **_kwargs: calls.append(("say", text)))

    parked = skill._approach_handle()

    assert parked == pytest.approx((0.40, -0.05285))
    assert calls[0][0] == "init"
    assert calls[0][2] is module._DOOR_APPROACH_PARAMS
    assert calls[0][3] == skill._detect_handle_floor_px
    assert calls[0][4] is debug
    assert calls[2:] == [
        ("search", "the protruding handle directly ahead"),
        ("position", "the protruding handle directly ahead", (0.8, 0.1)),
    ]


def test_parked_handle_accepts_arm_offset_target(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-base-test"))
    monkeypatch.setattr(skill, "_odom_xyt", lambda: (0.0, 0.0, 0.0))

    target = skill._parked_handle_target((0.38, -0.05285, 0.21725490235201467))

    assert target == pytest.approx((0.38, -0.05285, 0.21725490235201467))


def test_parked_handle_accepts_live_floor_approach_distance(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-live-floor-distance-test"))
    monkeypatch.setattr(skill, "_odom_xyt", lambda: (0.47, 0.06, 0.11))

    target = skill._parked_handle_target((0.45915, -0.0786, 0.2042))

    assert target == pytest.approx((0.45915, -0.0786, 0.2042))


@pytest.mark.parametrize("point", [(0.55, -0.05285, 0.22), (0.38, 0.10, 0.22)])
def test_parked_handle_rejects_target_outside_grasp_workspace(monkeypatch, point):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-base-reject-test"))
    monkeypatch.setattr(skill, "_odom_xyt", lambda: (0.0, 0.0, 0.0))

    with pytest.raises(SkillFailed, match="Floor approach did not leave the handle"):
        skill._parked_handle_target(point)


def test_wrist_decision_parser_requires_allowed_action_and_box():
    valid = '[{"box_2d":[200,450,800,550],"grasp_point":[500,500],"action":"forward","reason":"too far"}]'

    action, box, reason = module._parse_wrist_decision(valid)

    assert action == "FORWARD"
    assert box == (288, 96, 64, 288)
    assert reason == "too far"
    assert module._parse_wrist_decision('[{"action":"SIDEWAYS","box_2d":[200,450,800,550]}]') is None
    assert module._parse_wrist_decision('[{"action":"GRASP"}]') is None
    assert module._parse_wrist_decision('[{"action":"ABORT","reason":"not visible"}]') == (
        "ABORT",
        None,
        "not visible",
    )


def test_wrist_actions_are_ten_millimetres_and_bounded():
    start = (0.35, 0.01, 0.20)

    assert module._wrist_action_pose(start, "FORWARD") == pytest.approx((0.36, 0.01, 0.20))
    assert module._wrist_action_pose(start, "BACK") == pytest.approx((0.34, 0.01, 0.20))
    assert module._wrist_action_pose(start, "LEFT") == pytest.approx((0.35, 0.02, 0.20))
    assert module._wrist_action_pose(start, "RIGHT") == pytest.approx((0.35, 0.00, 0.20))
    assert module._wrist_action_pose((0.40, 0.10, 0.30), "FORWARD") == pytest.approx((0.40, 0.10, 0.30))


class _ActionManipulation:
    def __init__(self, *, z_lag=0.0):
        self.position = [0.35, 0.01, 0.20]
        self.moves = []
        self.z_lag = z_lag

    @property
    def pose(self):
        return SimpleNamespace(
            position=tuple(self.position),
            x=self.position[0],
            y=self.position[1],
            z=self.position[2],
        )

    def move_to(self, x, y, z, **_kwargs):
        self.moves.append((x, y, z))
        self.position[:] = [x, y, z - self.z_lag]
        return self.pose


def test_wrist_loop_executes_semantic_actions_until_grasp(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-action-test"))
    skill.manipulation = _ActionManipulation()
    skill.wrist_image = object()
    monkeypatch.setattr(skill, "sleep", lambda _seconds: None)
    monkeypatch.setattr(skill, "_effort", lambda: (0.0, 0.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr(skill, "_next_image", lambda _camera, _previous: object())
    decisions = iter(
        [
            ("FORWARD", (280.0, 80.0, 80.0, 320.0), "advance"),
            ("RIGHT", (280.0, 80.0, 80.0, 320.0), "move right"),
            ("GRASP", (280.0, 80.0, 80.0, 320.0), "between fingers"),
        ]
    )
    history = []

    def decide(_image, *, previous_image, previous_action, step):
        history.append((previous_image is not None, previous_action, step))
        return next(decisions)

    monkeypatch.setattr(skill, "_request_wrist_decision", decide)

    result = skill._wrist_align((0.35, 0.01, 0.20), restage=False)

    assert result == pytest.approx((0.36, 0.00, 0.20))
    assert skill.manipulation.moves == pytest.approx([(0.36, 0.01, 0.20), (0.36, 0.00, 0.20)])
    assert history == [(False, None, 0), (True, "FORWARD", 1), (True, "RIGHT", 2)]


def test_wrist_loop_accumulates_commands_without_adopting_measured_z_lag(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-commanded-pose-test"))
    skill.manipulation = _ActionManipulation(z_lag=0.002)
    skill.wrist_image = object()
    monkeypatch.setattr(skill, "sleep", lambda _seconds: None)
    monkeypatch.setattr(skill, "_effort", lambda: (0.0, 0.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr(skill, "_next_image", lambda _camera, _previous: object())
    decisions = iter(
        [
            ("UP", (280.0, 80.0, 80.0, 320.0), "raise"),
            ("UP", (280.0, 80.0, 80.0, 320.0), "raise again"),
            ("RIGHT", (280.0, 80.0, 80.0, 320.0), "move right"),
            ("GRASP", (280.0, 80.0, 80.0, 320.0), "between fingers"),
        ]
    )
    monkeypatch.setattr(skill, "_request_wrist_decision", lambda *_args, **_kwargs: next(decisions))

    result = skill._wrist_align((0.35, 0.01, 0.20), restage=False)

    expected_moves = [
        (0.35, 0.01, 0.210),
        (0.35, 0.01, 0.220),
        (0.35, 0.00, 0.220),
    ]
    assert len(skill.manipulation.moves) == len(expected_moves)
    for actual, expected in zip(skill.manipulation.moves, expected_moves, strict=True):
        assert actual == pytest.approx(expected)
    assert result == pytest.approx((0.35, 0.00, 0.218))


def test_wrist_loop_logs_but_does_not_abort_on_large_tracking_error(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-tracking-test"))
    skill.manipulation = _ActionManipulation(z_lag=0.020)
    skill.wrist_image = object()
    monkeypatch.setattr(skill, "sleep", lambda _seconds: None)
    monkeypatch.setattr(skill, "_effort", lambda: (0.0, 0.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr(skill, "_next_image", lambda _camera, _previous: object())
    decisions = iter(
        [
            ("UP", (280.0, 80.0, 80.0, 320.0), "raise"),
            ("UP", (280.0, 80.0, 80.0, 320.0), "raise again"),
            ("GRASP", (280.0, 80.0, 80.0, 320.0), "between fingers"),
        ]
    )
    monkeypatch.setattr(skill, "_request_wrist_decision", lambda *_args, **_kwargs: next(decisions))
    events = []
    monkeypatch.setattr(skill, "debug_event", lambda event, **fields: events.append((event, fields)))

    result = skill._wrist_align((0.35, 0.01, 0.20), restage=False)

    expected_moves = [(0.35, 0.01, 0.210), (0.35, 0.01, 0.220)]
    assert len(skill.manipulation.moves) == len(expected_moves)
    for actual, expected in zip(skill.manipulation.moves, expected_moves, strict=True):
        assert actual == pytest.approx(expected)
    assert result == pytest.approx((0.35, 0.01, 0.200))
    motion_events = [fields for event, fields in events if event == "wrist_action_motion"]
    assert [event["max_tracking_error_m"] for event in motion_events] == pytest.approx([0.020, 0.020])
    assert all("effort" in event for event in motion_events)


def test_wrist_loop_continues_past_twelve_actions(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-unbounded-actions-test"))
    skill.manipulation = _ActionManipulation()
    skill.wrist_image = object()
    monkeypatch.setattr(skill, "sleep", lambda _seconds: None)
    monkeypatch.setattr(skill, "_effort", lambda: (0.0, 0.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr(skill, "_next_image", lambda _camera, _previous: object())
    box = (280.0, 80.0, 80.0, 320.0)
    decisions = iter(
        [(action, box, "continue") for action in ("UP", "DOWN") * 6]
        + [("UP", box, "continue"), ("GRASP", box, "between fingers")]
    )
    monkeypatch.setattr(skill, "_request_wrist_decision", lambda *_args, **_kwargs: next(decisions))

    result = skill._wrist_align((0.35, 0.01, 0.20), restage=False)

    assert len(skill.manipulation.moves) == 13
    assert result == pytest.approx((0.35, 0.01, 0.210))


def test_forward_near_full_extension_retracts_arm_and_creeps_base(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-base-creep-test"))
    skill.manipulation = _ActionManipulation()
    skill.manipulation.position[:] = [0.37, 0.01, 0.20]
    skill.wrist_image = object()
    monkeypatch.setattr(skill, "sleep", lambda _seconds: None)
    monkeypatch.setattr(skill, "_effort", lambda: (1.0, 2.0, 3.0, 4.0, 5.0))
    monkeypatch.setattr(skill, "_next_image", lambda _camera, _previous: object())
    decisions = iter(
        [
            ("FORWARD", (280.0, 80.0, 80.0, 320.0), "advance with base"),
            ("GRASP", (280.0, 80.0, 80.0, 320.0), "shaft enclosed"),
        ]
    )
    monkeypatch.setattr(skill, "_request_wrist_decision", lambda *_args, **_kwargs: next(decisions))
    drives = []
    monkeypatch.setattr(skill, "_drive", drives.append)
    events = []
    monkeypatch.setattr(skill, "debug_event", lambda event, **fields: events.append((event, fields)))

    result = skill._wrist_align((0.35, 0.01, 0.20), restage=False)

    assert skill.manipulation.moves == pytest.approx([(0.33, 0.01, 0.20)])
    assert drives == pytest.approx([0.05])
    assert result == pytest.approx((0.33, 0.01, 0.20))
    creep = next(fields for event, fields in events if event == "wrist_base_creep")
    assert creep["arm_retraction_m"] == pytest.approx(0.04)
    assert creep["base_advance_m"] == pytest.approx(0.05)
    assert creep["net_forward_m"] == pytest.approx(0.01)


def test_grasp_uses_firm_strength_and_rejects_tip_pinch(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-grasp-test"))
    closes = []
    skill.manipulation = SimpleNamespace(gripper_close=lambda strength, **kwargs: closes.append((strength, kwargs)))
    skill.joint_states = SimpleNamespace(position=(0.0, 0.0, 0.0, 0.0, 0.0, 0.01))
    skill.wrist_image = object()
    request = {}

    def ask_image(_proxy, _image, prompt, **_kwargs):
        request["prompt"] = prompt
        return "NO"

    monkeypatch.setattr(module.gemlib, "ask_image", ask_image)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(skill, "_save_frame", lambda *_args: "verification.jpg")
    monkeypatch.setattr(skill, "debug_event", lambda *_args, **_kwargs: None)

    assert not skill._grasp((0.40, 0.0, 0.20), 1)
    assert closes == [(0.60, {"duration": 1.0})]
    assert "opposite sides" in request["prompt"]
    assert "Answer NO if the fingers merely pinch the terminal tip/end" in request["prompt"]


def test_grasp_retry_retracts_from_current_pose_after_base_creep(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-retry-retreat-test"))
    manipulation = _ActionManipulation()
    manipulation.position[:] = [0.39, 0.01, 0.20]
    manipulation.gripper_open = lambda **_kwargs: None
    skill.manipulation = manipulation
    monkeypatch.setattr(skill, "debug_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(skill, "_wrist_align", lambda target, **_kwargs: target)

    target = (0.20, 0.01, 0.20)  # Deliberately stale after a base advance.
    assert skill._prepare_grasp_retry(target, 2) == target
    assert len(manipulation.moves) == 1
    assert manipulation.moves[0] == pytest.approx((0.35, 0.01, 0.20))


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


def test_full_skill_acquires_before_pull_handoff(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("open-door-test"))
    skill.mobility = _Mobility()
    skill.manipulation = _Manipulation()
    skill.head = _Head()
    order = []

    def retry(target, attempt):
        order.append(("retry", attempt))
        return (0.36, target[1], target[2])

    parked = (0.40, -0.05285)
    monkeypatch.setattr(skill, "_approach_handle", lambda: order.append("approach") or parked)
    monkeypatch.setattr(
        skill,
        "_localize_handle",
        lambda floor: order.append(("localize", floor)) or (0.38, -0.05285, 0.2),
    )
    monkeypatch.setattr(
        skill,
        "_parked_handle_target",
        lambda _point: order.append("validate_park") or (0.38, -0.05285, 0.2),
    )
    monkeypatch.setattr(
        skill,
        "_wrist_align",
        lambda target: order.append("align") or (0.35, target[1], target[2]),
    )
    monkeypatch.setattr(
        skill,
        "_grasp",
        lambda pregrasp, attempt: order.append(("grasp", attempt, pregrasp)) or attempt == 2,
    )
    monkeypatch.setattr(skill, "_prepare_grasp_retry", retry)
    monkeypatch.setattr(
        skill,
        "_retreat_and_push_left",
        lambda retreat, left: order.append(("retreat_and_push_left", retreat, left)),
    )

    result = skill.execute(pull_distance_m=0.40)

    assert order == [
        "approach",
        ("localize", parked),
        "validate_park",
        "align",
        ("grasp", 1, (0.35, -0.05285, 0.2)),
        ("retry", 2),
        ("grasp", 2, (0.36, -0.05285, 0.2)),
        ("retreat_and_push_left", 0.40, 0.40),
    ]
    assert "pulled back 0.40 m arm-first toward x=0.25 m with a straight backward base remainder" in result
    assert "pulled left up to 0.40 m while gripping" in result
    assert "opened halfway" in result
    assert "backed up another 0.10 m" in result
    assert skill.mobility.stops == 1
    assert skill.manipulation.stops == 1
    assert skill.manipulation.setup[:2] == ["torque_on", "gripper_open"]
    assert skill.head.positions == [-20, 0, 0]


def test_post_grasp_retreat_pulls_left_before_half_release_and_final_retreat(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-left-push-test"))
    drives = []
    events = []
    monkeypatch.setattr(skill, "_drive", drives.append)
    sleeps = []
    monkeypatch.setattr(skill, "sleep", sleeps.append)
    monkeypatch.setattr(skill, "check_cancelled", lambda: None)
    monkeypatch.setattr(skill, "feedback", lambda _message: None)
    monkeypatch.setattr(skill, "debug_event", lambda event, **fields: events.append((event, fields)))
    skill.joint_states = SimpleNamespace(position=(0.1, 0.02, 0.3, 0.4, 0.5, -0.6))

    class PushManipulation:
        def __init__(self):
            self.actions = []
            self.pose = SimpleNamespace(x=0.35, y=-0.04, z=0.20, position=(0.35, -0.04, 0.20), rpy=(0.1, 0.2, 0.3))

        def move_to(self, x, y, z, **kwargs):
            self.actions.append(("move_to", (x, y, z), kwargs))
            self.pose = SimpleNamespace(x=x, y=y, z=z, position=(x, y, z), rpy=(0.1, 0.2, 0.3))
            return self.pose

        def stream_to(self, x, y, z, **kwargs):
            self.actions.append(("stream_to", (x, y, z), kwargs))
            self.pose = SimpleNamespace(x=x, y=y, z=z, position=(x, y, z), rpy=(0.1, 0.2, 0.3))

        def stream_keepalive(self):
            self.actions.append(("keepalive",))

        def gripper_open(self, **kwargs):
            self.actions.append(("open", kwargs))

        def stream_stop(self):
            self.actions.append(("stop",))

    skill.manipulation = PushManipulation()

    skill._retreat_and_push_left(0.30, 0.30)

    assert drives == pytest.approx([-0.20, -0.10])
    arm_pull = skill.manipulation.actions[0]
    assert arm_pull[0] == "move_to"
    assert arm_pull[1] == pytest.approx((0.25, -0.04, 0.20))
    assert arm_pull[2] == {
        "roll": 0.1,
        "pitch": 0.2,
        "yaw": 0.3,
        "duration": 2.0,
        "tolerance_xy": None,
        "tolerance_z": None,
    }
    streams = [action for action in skill.manipulation.actions if action[0] == "stream_to"]
    assert len(streams) == 30
    assert [action[1][0] for action in streams] == pytest.approx([0.25] * 30)
    assert [action[1][1] for action in streams] == pytest.approx(
        [-0.04 + offset for offset in (0.01 * step for step in range(1, 31))]
    )
    assert [action[1][2] for action in streams] == pytest.approx([0.20] * 30)
    assert all(action[2]["max_speed"] == pytest.approx(0.25) for action in streams)
    assert all(action[2]["roll"] == pytest.approx(0.1) for action in streams)
    assert all(action[2]["pitch"] == pytest.approx(0.2) for action in streams)
    assert all(action[2]["yaw"] == pytest.approx(0.3) for action in streams)
    assert sum(sleeps) == pytest.approx(5.0)
    assert skill.manipulation.actions[-2] == ("stop",)
    assert skill.manipulation.actions[-1] == ("open", {"percent": 50.0, "duration": 1.0})
    assert [event for event, _fields in events].count("left_push_step") == 30
    started = next(fields for event, fields in events if event == "left_push_started")
    assert started["strategy"] == "cartesian_ik_positive_y"
    assert started["origin_pose"] == pytest.approx([0.25, -0.04, 0.20])
    assert started["final_target"] == pytest.approx([0.25, 0.26, 0.20])
    assert started["requested_left_distance_m"] == pytest.approx(0.30)
    assert started["gripper_state"] == "closed"
    completed = next(fields for event, fields in events if event == "left_push_complete")
    assert completed["measured_left_distance_m"] == pytest.approx(0.30)
    assert completed["last_requested_left_distance_m"] == pytest.approx(0.30)
    arm_completed = next(fields for event, fields in events if event == "arm_pull_complete")
    assert arm_completed["measured_arm_retraction_m"] == pytest.approx(0.10)
    assert arm_completed["base_remainder_m"] == pytest.approx(0.20)
    assert arm_completed["measured_joint2_rad"] == pytest.approx(0.02)
    assert arm_completed["measured_joint2_deg"] == pytest.approx(math.degrees(0.02))


def test_post_grasp_left_pull_stops_at_ik_reach_boundary(monkeypatch):
    monkeypatch.setattr(OpenDoorWithVision, "_proxy", object())
    skill = OpenDoorWithVision(logging.getLogger("door-left-reach-test"))
    drives = []
    events = []
    monkeypatch.setattr(skill, "_drive", drives.append)
    monkeypatch.setattr(skill, "sleep", lambda _seconds: None)
    monkeypatch.setattr(skill, "check_cancelled", lambda: None)
    monkeypatch.setattr(skill, "feedback", lambda _message: None)
    monkeypatch.setattr(skill, "debug_event", lambda event, **fields: events.append((event, fields)))
    skill.joint_states = SimpleNamespace(position=(0.0, 0.0, 0.0, 0.0, 0.0, -0.6))

    class ReachLimitedManipulation:
        def __init__(self):
            self.actions = []
            self.pose = SimpleNamespace(x=0.35, y=0.0, z=0.20, position=(0.35, 0.0, 0.20), rpy=(0.0, 0.0, 0.0))

        def move_to(self, x, y, z, **kwargs):
            self.actions.append(("move_to", (x, y, z), kwargs))
            self.pose = SimpleNamespace(x=x, y=y, z=z, position=(x, y, z), rpy=(0.0, 0.0, 0.0))
            return self.pose

        def stream_to(self, x, y, z, **kwargs):
            if y > 0.02:
                raise module.ArmFailed(f"IK found no solution for streaming target ({x:.2f}, {y:.2f}, {z:.2f})")
            self.actions.append(("stream_to", (x, y, z), kwargs))
            self.pose = SimpleNamespace(x=x, y=y, z=z, position=(x, y, z), rpy=(0.0, 0.0, 0.0))

        def stream_keepalive(self):
            self.actions.append(("keepalive",))

        def stream_stop(self):
            self.actions.append(("stop",))

        def gripper_open(self, **kwargs):
            self.actions.append(("open", kwargs))

    skill.manipulation = ReachLimitedManipulation()

    skill._retreat_and_push_left(0.30, 0.30)

    assert drives == pytest.approx([-0.20, -0.10])
    streams = [action for action in skill.manipulation.actions if action[0] == "stream_to"]
    assert [action[1][1] for action in streams] == pytest.approx([0.01, 0.02])
    limit = next(fields for event, fields in events if event == "left_push_reach_limit")
    assert limit["failed_offset_m"] == pytest.approx(0.03)
    assert limit["last_requested_offset_m"] == pytest.approx(0.02)
    assert skill.manipulation.actions[-1] == ("open", {"percent": 50.0, "duration": 1.0})


def test_door_defaults_to_thirty_centimetres_back_and_left(monkeypatch):
    assert OpenDoorWithVision.execute.__defaults__[-1] == pytest.approx(0.30)
