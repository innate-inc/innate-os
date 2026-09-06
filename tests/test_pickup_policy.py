import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "pickup_policy", Path(__file__).resolve().parents[1] / "workspace/innate_skills/pickup_policy.py"
)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def test_observation_rejects_invalid_boxes_and_unsafe_grasp_choices():
    plans = {
        "head": dict(box_2d=[100, 200, 300, 400], grip_strength=0.35, search_clearance="low"),
        "wrist": dict(box_2d=[100, 200, 300, 400], axis_2d=[200, 220, 200, 380], grasp_point_2d=[200, 300]),
    }
    for view, plan in plans.items():
        assert policy.validate_observation({"detections": [plan]}, view)["detections"] == [plan]
        assert policy.validate_observation({"detections": []}, view) == {"detections": []}
        for update in (
            {"box_2d": [100, 200, 100, 400]},
            {"box_2d": [-1, 0, 200, 200]},
            {"box_2d": [0, 0, float("nan"), 20]},
            {"axis_2d": [100, 100, 100, 100]},
            {"axis_2d": [0, 0, 1001, 100]},
            {"grasp_point_2d": [200, 500]},
            {"grasp_point_2d": [float("nan"), 300]},
            {"grasp_point_2d": [100, 300]},
            {"grip_strength": 0.7},
            {"search_clearance": "unsafe"},
        ):
            with pytest.raises(ValueError):
                policy.validate_observation({"detections": [{**plan, **update}]}, view)


def test_cancelled_inference_cannot_return_late_action():
    release = threading.Event()
    finished = threading.Event()

    def transport(*_args):
        release.wait(2)
        finished.set()
        yield {
            "type": "response.completed",
            "response": {"status": "completed", "usage": {"input_tokens": 1}, "output": []},
        }

    def cancel(_seconds):
        raise InterruptedError("stop")

    instance = policy.PickupPolicy(transport)
    with pytest.raises(InterruptedError):
        instance.locate("brick", "jpeg", cancel, lambda: None, view="wrist")
    release.set()
    assert finished.wait(2)


def test_incomplete_response_and_call_budget_fail_without_actions():
    instance = policy.PickupPolicy(lambda *_args: iter([]), max_calls=1)
    with pytest.raises(ValueError, match="no action"):
        instance.locate("brick", "jpeg", time.sleep, lambda: None, view="wrist")
    with pytest.raises(ValueError, match="budget"):
        instance.locate("brick", "jpeg", time.sleep, lambda: None, view="wrist")


def test_wrist_identity_reference_keeps_current_image_first_and_head_coordinates_separate():
    bodies = []

    def transport(_model, body):
        bodies.append(body)
        yield {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "usage": {"input_tokens": 1},
                "output": [{"type": "function_call", "name": "pickup_observation", "arguments": '{"detections":[]}'}],
            },
        }

    instance = policy.PickupPolicy(transport)
    reference = {"image": "head-jpeg", "box_2d": [100, 200, 300, 400]}
    instance.locate("red cube", "wrist-jpeg", time.sleep, lambda: None, view="wrist", reference=reference)
    content = bodies[0]["input"][0]["content"]
    assert json.loads(content[0]["text"])["head_reference_box_2d"] == reference["box_2d"]
    assert [item["image_url"].split(",")[1] for item in content[1:]] == ["wrist-jpeg", "head-jpeg"]
    instance.locate("red cube", "new-head-jpeg", time.sleep, lambda: None, view="head", reference=reference)
    assert len(bodies[1]["input"][0]["content"]) == 2  # head localization has no stale reference

    wrist_fields = bodies[0]["tools"][0]["parameters"]["properties"]["detections"]["items"]["required"]
    head_fields = bodies[1]["tools"][0]["parameters"]["properties"]["detections"]["items"]["required"]
    assert set(wrist_fields) == {"box_2d", "axis_2d", "grasp_point_2d"}
    assert set(head_fields) == {"box_2d", "grip_strength", "search_clearance"}


def test_metric_views_require_upper_material_point_and_boolean_final_veto():
    head = dict(box_2d=[100, 200, 300, 400], grip_strength=0.35, search_clearance="low", grasp_point_2d=[130, 300])
    assert policy.validate_observation({"detections": [head]}, "head_metric")
    for bad in ([100, 300], [200, 500], [], [float("nan"), 300]):
        with pytest.raises(ValueError):
            policy.validate_observation({"detections": [{**head, "grasp_point_2d": bad}]}, "head_metric")
    wrist = dict(box_2d=[100, 200, 300, 400], aligned=False)
    assert policy.validate_observation({"detections": [wrist]}, "wrist_verify")
    for bad in ("true", 1, None):
        with pytest.raises(ValueError):
            policy.validate_observation({"detections": [{**wrist, "aligned": bad}]}, "wrist_verify")
