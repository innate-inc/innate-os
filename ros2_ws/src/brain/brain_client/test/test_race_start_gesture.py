# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Exercise the shipped gesture with a fake arm; no robot commands are sent."""

import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("rclpy")
from brain_client.skills.types import Skill, SkillCancelled  # noqa: E402


@pytest.mark.parametrize("cancel_at", [None, "ready", "signal"])
def test_gesture_orders_speech_and_always_stops_stream(cancel_at):
    source = Path(__file__).resolve().parents[5] / "workspace/innate_skills/race_start_gesture.py"
    spec = importlib.util.spec_from_file_location("gesture_under_test", source)
    module = importlib.util.module_from_spec(spec)
    registry = dict(Skill._registry)
    try:
        spec.loader.exec_module(module)
        skill = module.RaceStartGesture(logging.getLogger("gesture_test"))
        events = []
        phases = {
            tuple(module.READY_POSE): "ready",
            tuple(module.SIGNAL_POSE): "signal",
            tuple(module.REST_POSE): "rest",
        }

        def stream(pose, **kwargs):
            # Five joint targets preserve the gripper's existing command.
            assert len(pose) == 5
            phase = phases[tuple(pose)]
            events.append(phase)
            if phase == cancel_at:
                skill.cancel()

        skill.manipulation = SimpleNamespace(stream_joints=stream, stream_stop=lambda: events.append("stop"))
        skill.say = lambda text: events.append(text)
        skill.feedback = lambda message: None
        # Keep the real cancel latch while removing wall-clock delays.
        skill.sleep = lambda seconds: skill.check_cancelled()
        if cancel_at:
            with pytest.raises(SkillCancelled):
                skill.execute()
            assert "rest" not in events
            assert ("GO!" in events) == (cancel_at == "signal")
        else:
            assert "completed" in skill.execute()
            assert events.count("GO!") == 1
            assert events.index("ready") < events.index("GO!") < events.index("signal") < events.index("rest")
        assert events[-1] == "stop" and events.count("stop") == 1
    finally:
        Skill._registry.clear()
        Skill._registry.update(registry)
