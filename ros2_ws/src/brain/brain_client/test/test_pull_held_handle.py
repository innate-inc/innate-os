# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import importlib
import json
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

pytest.importorskip("rclpy")

from brain_client.skills import debug_runs
from brain_client.skills.types import SkillFailed
from brain_client.state.joint_states import JointStates

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
PullHeldHandle = importlib.import_module("workspace.innate_skills.arm.pull_held_handle").PullHeldHandle


@dataclass
class _Pose:
    x: float = 0.2
    y: float = 0.0
    z: float = 0.2
    rpy: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def position(self):
        return self.x, self.y, self.z


class _Manipulation:
    def __init__(self):
        self.pose = _Pose()
        self.stops = 0

    def stream_to(self, x, y, z, **_kwargs):
        self.pose = _Pose(x, y, z)

    def stream_stop(self):
        self.stops += 1


def test_pull_held_handle_runs_bounded_stream_stops_and_records_decisions(tmp_path, monkeypatch):
    monkeypatch.setattr(debug_runs, "get_workspace_dir", lambda: tmp_path)
    skill = PullHeldHandle(logging.getLogger("pull-test"))
    skill._configure_debug_run(
        run_id="pull-test-run",
        skill_id="innate-os/pull-held-handle",
        inputs={"distance_m": 0.012},
    )
    skill.manipulation = _Manipulation()
    skill.joint_states = JointStates(
        name=("joint1", "joint2", "joint3", "joint4", "joint5", "joint6"),
        position=(0.0,) * 6,
        velocity=(0.0,) * 6,
        effort=(5.0, -4.0, 3.0, 2.0, 1.0, 40.0),
        received_at=time.monotonic(),
    )
    skill.sleep = lambda _seconds: None

    result = skill.execute(distance_m=0.012)
    skill._finish_debug_run(status="success", message=result)

    assert "Pulled the held handle 0.01 m" in result
    assert skill.manipulation.pose.x == pytest.approx(0.188)
    assert skill.manipulation.stops == 1
    events = [json.loads(line) for line in (skill.debug_directory / "events.jsonl").read_text().splitlines()]
    event_names = [event["event"] for event in events]
    assert "tare_sample" in event_names
    assert event_names.count("step_decision") == 2
    assert event_names.count("step_observation") == 2
    assert event_names[-1] == "run_finished"


def test_stale_feedback_brakes_before_recording_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(debug_runs, "get_workspace_dir", lambda: tmp_path)
    skill = PullHeldHandle(logging.getLogger("pull-stale-test"))
    skill._configure_debug_run(run_id="stale-run", skill_id="innate-os/pull-held-handle", inputs={})
    skill.manipulation = _Manipulation()
    skill.joint_states = JointStates(
        name=("joint1", "joint2", "joint3", "joint4", "joint5", "joint6"),
        position=(0.0,) * 6,
        velocity=(0.0,) * 6,
        effort=(0.0,) * 6,
        received_at=0.0,
    )
    skill.sleep = lambda _seconds: None

    with pytest.raises(SkillFailed, match="stale"):
        skill.execute(distance_m=0.01)

    events = [json.loads(line) for line in (skill.debug_directory / "events.jsonl").read_text().splitlines()]
    names = [event["event"] for event in events]
    assert skill.manipulation.stops == 1
    assert names.index("safety_stop") < names.index("failure_requested")
