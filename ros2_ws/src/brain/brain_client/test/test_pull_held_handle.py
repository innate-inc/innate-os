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
    assert skill.manipulation.pose.x == pytest.approx(0.212)
    assert skill.manipulation.stops == 1
    events = [json.loads(line) for line in (skill.debug_directory / "events.jsonl").read_text().splitlines()]
    event_names = [event["event"] for event in events]
    assert "tare_sample" in event_names
    assert event_names.count("step_decision") == 2
    assert event_names.count("step_observation") == 2
    assert event_names[-1] == "run_finished"
    decisions = [event for event in events if event["event"] == "step_decision"]
    assert all(event["target_rpy"] == [0.0, 0.0, 0.0] for event in decisions)


def test_pull_held_handle_locks_starting_orientation_and_counts_axis_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(debug_runs, "get_workspace_dir", lambda: tmp_path)
    skill = PullHeldHandle(logging.getLogger("pull-axis-test"))
    skill._configure_debug_run(run_id="axis-run", skill_id="innate-os/pull-held-handle", inputs={})

    class DriftingManipulation(_Manipulation):
        def __init__(self):
            super().__init__()
            self.pose = _Pose(rpy=(0.1, 0.2, 0.3))
            self.commands = []

        def stream_to(self, x, y, z, **kwargs):
            self.commands.append((x, y, z, kwargs))
            self.pose = _Pose(x, y + 0.002, z, rpy=(0.8, 0.9, 1.0))

    skill.manipulation = DriftingManipulation()
    skill.joint_states = JointStates(
        name=("joint1", "joint2", "joint3", "joint4", "joint5", "joint6"),
        position=(0.0,) * 6,
        velocity=(0.0,) * 6,
        effort=(0.0,) * 6,
        received_at=time.monotonic(),
    )
    skill.sleep = lambda _seconds: None

    result = skill.execute(distance_m=0.012, direction_x=1.0)

    assert "0.01 m" in result
    assert skill.manipulation.pose.x == pytest.approx(0.212)
    assert skill.manipulation.pose.y == pytest.approx(0.004)
    assert all(command[3]["roll"] == 0.1 for command in skill.manipulation.commands)
    assert all(command[3]["pitch"] == 0.2 for command in skill.manipulation.commands)
    assert all(command[3]["yaw"] == 0.3 for command in skill.manipulation.commands)


def test_pull_held_handle_accepts_submillimeter_completion_remainder(tmp_path, monkeypatch):
    monkeypatch.setattr(debug_runs, "get_workspace_dir", lambda: tmp_path)
    skill = PullHeldHandle(logging.getLogger("pull-tolerance-test"))
    skill._configure_debug_run(run_id="tolerance-run", skill_id="innate-os/pull-held-handle", inputs={})

    class SlightlyUndertrackingManipulation(_Manipulation):
        def stream_to(self, x, y, z, **_kwargs):
            self.pose = _Pose(x - 0.0002, y, z)

    skill.manipulation = SlightlyUndertrackingManipulation()
    skill.joint_states = JointStates(
        name=("joint1", "joint2", "joint3", "joint4", "joint5", "joint6"),
        position=(0.0,) * 6,
        velocity=(0.0,) * 6,
        effort=(0.0,) * 6,
        received_at=time.monotonic(),
    )
    skill.sleep = lambda _seconds: None

    result = skill.execute(distance_m=0.01, direction_x=1.0)

    assert "Pulled the held handle 0.01 m" in result
    assert skill.manipulation.pose.x == pytest.approx(0.2098)


def test_pull_held_handle_stops_on_vertical_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(debug_runs, "get_workspace_dir", lambda: tmp_path)
    skill = PullHeldHandle(logging.getLogger("pull-drift-test"))
    skill._configure_debug_run(run_id="drift-run", skill_id="innate-os/pull-held-handle", inputs={})

    class VerticallyDriftingManipulation(_Manipulation):
        def stream_to(self, x, y, z, **_kwargs):
            self.pose = _Pose(x, y, z - 0.02)

    skill.manipulation = VerticallyDriftingManipulation()
    skill.joint_states = JointStates(
        name=("joint1", "joint2", "joint3", "joint4", "joint5", "joint6"),
        position=(0.0,) * 6,
        velocity=(0.0,) * 6,
        effort=(0.0,) * 6,
        received_at=time.monotonic(),
    )
    skill.sleep = lambda _seconds: None

    with pytest.raises(SkillFailed, match="drift exceeded"):
        skill.execute(distance_m=0.01, direction_x=1.0)

    events = [json.loads(line) for line in (skill.debug_directory / "events.jsonl").read_text().splitlines()]
    stop = next(event for event in events if event["event"] == "safety_stop")
    assert stop["reason"] == "trajectory_drift"
    assert stop["vertical_drift_m"] > stop["vertical_drift_limit_m"]


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
