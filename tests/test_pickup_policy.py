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
    plan = dict(box_2d=[100, 200, 300, 400], roll=-1.5, grip_strength=0.35, grasp_style="floor", search_clearance="low")
    assert policy.validate_observation({"detections": [plan]})["detections"] == [plan]
    assert policy.validate_observation({"detections": []}) == {"detections": []}
    for update in (
        {"box_2d": [100, 200, 100, 400]},
        {"box_2d": [-1, 0, 200, 200]},
        {"box_2d": [0, 0, float("nan"), 20]},
        {"roll": 1.6},
        {"grip_strength": 0.7},
        {"grasp_style": "drop"},
        {"search_clearance": "unsafe"},
    ):
        with pytest.raises(ValueError):
            policy.validate_observation({"detections": [{**plan, **update}]})


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
