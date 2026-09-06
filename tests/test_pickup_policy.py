import importlib.util
import threading
import time
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "pickup_policy", Path(__file__).resolve().parents[1] / "workspace/innate_skills/pickup_policy.py"
)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)

PARAMS = dict(
    wrist_final_half_px=20,
    wrist_half_px=60,
    wrist_box_u=320,
    wrist_box_v=310,
    wrist_stop_z=0.05,
    hover_z=0.15,
    wrist_move_s=0.5,
    wrist_step_max=0.04,
    wrist_z_step=0.01,
)


def action(**updates):
    return dict(action="move", center=[320, 310], delta=[0, 0, -0.04], roll=0, note="Visible brick", **updates)


def test_motion_preserves_rates_and_rejects_unsafe_model_choices():
    assert policy.motion_target(action(), (0.3, 0, 0.15), PARAMS) == pytest.approx((0.3, 0, 0.11, 2))
    assert policy.motion_target(action(), (0.3, 0, 0.27), {**PARAMS, "wrist_ceiling_z": 0.27}) == pytest.approx(
        (0.3, 0, 0.23, 2)
    )
    descent = action()
    descent.update(action="descend", delta=[0, 0, -0.10])
    assert policy.motion_target(descent, (0.3, 0, 0.15), PARAMS) == pytest.approx((0.3, 0, 0.05, 5))
    correction = action()
    correction.update(delta=[0, 0.01, 0])
    assert policy.motion_target(correction, (0.3, 0, 0.048), PARAMS) == pytest.approx((0.3, 0.01, 0.048, 0.5))
    for change in (
        {"delta": [0, 0, -0.05]},
        {"delta": [float("nan"), 0, 0]},
        {"center": [240, 310]},
        {"delta": [0.01, 0, -0.04]},
        {"action": "grasp", "delta": [0, 0, 0]},
    ):
        value = action()
        value.update(change)
        with pytest.raises(ValueError):
            policy.motion_target(value, (0.3, 0, 0.15), PARAMS)
    value = action()
    value.update(action="grasp", delta=[0, 0, 0])
    assert policy.motion_target(value, (0.3, 0, 0.05), PARAMS) is None


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
        instance.decide({}, "jpeg", cancel, lambda: None)
    release.set()
    assert finished.wait(2)


def test_incomplete_response_and_call_budget_fail_without_actions():
    instance = policy.PickupPolicy(lambda *_args: iter([]), max_calls=1)
    with pytest.raises(ValueError, match="no action"):
        instance.decide({}, "jpeg", time.sleep, lambda: None)
    with pytest.raises(ValueError, match="budget"):
        instance.decide({}, "jpeg", time.sleep, lambda: None)
