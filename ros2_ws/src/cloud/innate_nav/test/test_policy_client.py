"""The wire format, from the robot's side.

A protocol with two implementations drifts, and it did: the server grew
history_indices and this client's strict Plan(**d) turned that into a dead
receive thread and a robot that never moved. These pin both directions.
"""

from __future__ import annotations

import json

import pytest
from innate_nav.policy_client import Plan, Pose, encode_frame, ws_url

FULL = {
    "waypoints_m": [[0.25, 0.0, 0.0]],
    "capture_pose": {"x": 1.0, "y": 2.0, "yaw": 0.5},
    "capture_stamp": 3.0,
    "max_reach_m": 0.25,
    "stop": False,
    "p_stop": None,
    "keyframes": 4,
    "history_indices": [0, 2, 3],
    "history_stamps": [1.0, 2.5, 3.0],
    "seq": 7,
    "compute_ms": 310.0,
    "model": "ckpt",
}


def test_a_plan_parses_with_its_capture_pose_typed():
    plan = Plan.from_json(json.dumps(FULL))
    assert isinstance(plan.capture_pose, Pose)
    assert plan.history_stamps == [1.0, 2.5, 3.0]
    assert plan.seq == 7


def test_a_field_this_client_does_not_know_is_ignored_not_fatal():
    """The failure that motivated this: a newer server adding a field must not
    kill the receive thread and leave the robot waiting for plans forever."""
    plan = Plan.from_json(json.dumps({**FULL, "brand_new_field": 42}))
    assert plan.seq == 7


def test_a_missing_field_is_a_clear_error_rather_than_a_confusing_one():
    lean = {k: v for k, v in FULL.items() if k != "history_stamps"}
    with pytest.raises(ValueError, match="history_stamps"):
        Plan.from_json(json.dumps(lean))


def test_frames_are_framed_length_header_then_json_then_jpeg():
    jpeg = b"\xff\xd8not-really-a-jpeg\xff\xd9"
    buf = encode_frame(jpeg, Pose(1.5, -0.25, 0.75), 12.5)
    n = int.from_bytes(buf[:4], "big")
    head = json.loads(buf[4:4 + n])
    assert buf[4 + n:] == jpeg
    assert head["stamp"] == 12.5
    assert head["pose"] == {"x": 1.5, "y": -0.25, "yaw": 0.75}


def test_the_stream_url_follows_the_scheme_the_client_dialed():
    assert ws_url("http://10.0.0.4:8900", "/v1/x") == "ws://10.0.0.4:8900/v1/x"
    assert ws_url("https://nav.example.com/", "v1/x") == "wss://nav.example.com/v1/x"
    with pytest.raises(ValueError):
        ws_url("10.0.0.4:8900", "/v1/x")
