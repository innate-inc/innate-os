# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Orchestration tests for the autonomous mapping skill.

The geometry algorithm has its own ROS-free suite. These tests pin the skill's
mode handoff, Nav2 call contract, bounded failure policy, and human takeover.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(_REPO_ROOT / "workspace"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ros_skill_test_stubs import install_if_ros_is_missing  # noqa: E402

install_if_ros_is_missing()

from geometry_msgs.msg import Twist  # noqa: E402
from innate_skills import explore_map as explore_module  # noqa: E402
from innate_skills._frontier_planner import FrontierGoal  # noqa: E402
from innate_skills.explore_map import AUTONOMOUS_MAPPING_MODE, ExploreMap, _TeleopTakeover  # noqa: E402
from innate_skills.navigate_to_position import NavigationBlocked  # noqa: E402

from innate import NavMode, Pose, SkillCancelled, SkillFailed  # noqa: E402


def _map_state(grid: np.ndarray | None = None):
    if grid is None:
        grid = np.zeros((20, 20), dtype=np.int8)
    return SimpleNamespace(
        grid=grid,
        resolution=0.1,
        origin_x=0.0,
        origin_y=0.0,
        origin_theta=0.0,
        frame_id="map",
    )


def _goal(index: int = 0) -> FrontierGoal:
    x = 1.0 + 2.0 * index
    return FrontierGoal(
        row=10,
        col=10 + index,
        x=x,
        y=1.0,
        yaw=0.25,
        frontier_cells=10,
        route_distance_m=1.5,
        score=0.5,
        cluster_anchor=(5, 5 + index),
    )


class _Teleop:
    armed = False
    taken_over = False


class _Navigator:
    def __init__(self, failure: str | None = None):
        self.failure = failure
        self.calls = []

    def go_to_position(self, x, y, yaw, local_frame, *, timeout_s, safety_check):
        self.calls.append((x, y, yaw, local_frame, timeout_s, safety_check))
        safety_check()
        if self.failure:
            if isinstance(self.failure, SkillFailed):
                raise self.failure
            raise SkillFailed(self.failure)


class _RecoveryHint:
    def __init__(self):
        self.hints = 0

    def mark_front_obstacle(self):
        self.hints += 1


class _ModeController:
    def __init__(self, skill: ExploreMap, new_map=None, new_pose=None):
        self.skill = skill
        self.new_map = new_map
        self.new_pose = new_pose
        self.calls = 0

    def switch_to_autonomous_mapping(self):
        self.calls += 1
        self.skill.nav_mode = NavMode(AUTONOMOUS_MAPPING_MODE)
        if self.new_map is not None:
            self.skill.map = self.new_map
        if self.new_pose is not None:
            self.skill.pose = self.new_pose


def _skill(*, mode: str = AUTONOMOUS_MAPPING_MODE, map_state=None) -> ExploreMap:
    skill = ExploreMap(logging.getLogger("test_explore_map"))
    skill.nav_mode = NavMode(mode)
    skill.map = map_state or _map_state()
    skill.pose = Pose(x=0.55, y=0.55, theta=0.0)
    skill.lidar = SimpleNamespace(frame_id="laser")
    skill.__dict__["teleop"] = _Teleop()
    skill.__dict__["navigator"] = _Navigator()
    skill.__dict__["recovery_hint"] = _RecoveryHint()
    skill.__dict__["mode_controller"] = _ModeController(skill)
    skill.feedback = lambda _message: None
    skill.sleep = lambda _seconds: skill.check_cancelled()
    return skill


def test_switches_from_manual_mapping_then_sends_exact_map_frame_goal(monkeypatch):
    previous_map = _map_state()
    fresh_map = _map_state()
    skill = _skill(mode="mapping", map_state=previous_map)
    fresh_pose = Pose(x=0.6, y=0.6, theta=0.0, stamp=2.0)
    controller = _ModeController(skill, fresh_map, fresh_pose)
    navigator = _Navigator()
    skill.__dict__["mode_controller"] = controller
    skill.__dict__["navigator"] = navigator
    monkeypatch.setattr(explore_module, "find_frontier_goals", lambda *args, **kwargs: [_goal()])

    result = skill.execute(max_frontiers=1)

    assert controller.calls == 1
    assert len(navigator.calls) == 1
    x, y, yaw, local_frame, timeout_s, safety_check = navigator.calls[0]
    assert (x, y, yaw, local_frame) == (1.0, 1.0, 0.25, False)
    assert 89.0 <= timeout_s <= 90.0
    assert callable(safety_check)
    assert result.data["reached_frontiers"] == 1
    assert result.data["mode"] == AUTONOMOUS_MAPPING_MODE


def test_mode_switch_requires_a_genuinely_new_mapping_pose():
    skill = _skill(mode="mapping")
    previous_map = skill.map
    previous_pose = skill.pose
    skill.nav_mode = NavMode(AUTONOMOUS_MAPPING_MODE)
    skill.map = _map_state()
    # Make wait_for deterministic: the stale pose predicate returns None.
    skill.wait_for = lambda read, timeout: read()

    with pytest.raises(SkillFailed, match="fresh map-frame mapping pose"):
        skill._wait_for_mapping_state(previous_map, previous_pose, require_new_state=True)


def test_unchanged_mapping_pose_trips_freshness_guard(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(explore_module.time, "monotonic", lambda: now[0])
    skill = _skill()
    skill._arm_safety(skill.map, skill.pose, skill.lidar)

    now[0] = explore_module.POSE_STALE_TIMEOUT_S + 0.01
    with pytest.raises(SkillCancelled, match="pose stopped updating"):
        skill._safety_check()


def test_new_pose_wrappers_with_a_frozen_tf_stamp_are_still_stale(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(explore_module.time, "monotonic", lambda: now[0])
    skill = _skill()
    skill.pose = Pose(x=0.55, y=0.55, theta=0.0, stamp=42.0)
    skill._arm_safety(skill.map, skill.pose, skill.lidar)

    # mode_manager may publish a new Odometry wrapper on every wheel-odom
    # sample. It must not refresh safety unless the underlying TF stamp moved.
    skill.pose = Pose(x=0.55, y=0.55, theta=0.0, stamp=42.0)
    now[0] = explore_module.POSE_STALE_TIMEOUT_S + 0.01
    with pytest.raises(SkillCancelled, match="pose stopped updating"):
        skill._safety_check()


def test_teleop_takeover_monitors_mux_input_and_latches_only_when_armed():
    subscriptions = []

    class Node:
        def create_subscription(self, message_type, topic, callback, qos):
            subscription = SimpleNamespace(message_type=message_type, topic=topic, callback=callback, qos=qos)
            subscriptions.append(subscription)
            return subscription

        def destroy_subscription(self, _subscription):
            pass

    cancel_calls = []
    monitor = _TeleopTakeover(SimpleNamespace(node=Node(), cancel=lambda: cancel_calls.append(True)))
    assert subscriptions[0].topic == "/cmd_vel_teleop"

    moving = Twist()
    moving.linear.x = 0.2
    subscriptions[0].callback(moving)
    assert monitor.taken_over is False
    assert cancel_calls == []
    monitor.armed = True
    subscriptions[0].callback(moving)
    assert monitor.taken_over is True
    assert cancel_calls == [True]
    subscriptions[0].callback(moving)
    assert cancel_calls == [True]


def test_human_takeover_during_planning_cancels_before_navigation(monkeypatch):
    skill = _skill()
    navigator = _Navigator()
    skill.__dict__["navigator"] = navigator

    def planner(*args, check_cancelled, **kwargs):
        skill.teleop.taken_over = True
        check_cancelled()
        return [_goal()]

    monkeypatch.setattr(explore_module, "find_frontier_goals", planner)

    with pytest.raises(SkillCancelled, match="joystick takeover"):
        skill.execute(max_frontiers=1)
    assert navigator.calls == []


def test_three_navigation_failures_end_the_run(monkeypatch):
    skill = _skill()
    navigator = _Navigator(failure=NavigationBlocked("route blocked"))
    skill.__dict__["navigator"] = navigator

    def planner(*args, excluded_world, **kwargs):
        return [_goal(len(excluded_world))]

    monkeypatch.setattr(explore_module, "find_frontier_goals", planner)

    with pytest.raises(SkillFailed, match="3 navigation failures"):
        skill.execute(max_frontiers=10)
    assert len(navigator.calls) == 3
    assert skill.recovery_hint.hints == 2


def test_no_frontier_completes_only_after_two_fresh_map_updates(monkeypatch):
    skill = _skill()
    navigator = _Navigator()
    skill.__dict__["navigator"] = navigator
    map_reads = []

    def planner(*args, **kwargs):
        map_reads.append(skill.map)
        return []

    def advance_map(_seconds):
        skill.map = _map_state()
        skill.check_cancelled()

    skill.sleep = advance_map
    monkeypatch.setattr(explore_module, "find_frontier_goals", planner)

    result = skill.execute(max_frontiers=10)

    assert len(map_reads) == 2
    assert navigator.calls == []
    assert result.data["stop_reason"] == "map coverage complete"


def test_mode_change_cancels_before_any_goal_is_sent(monkeypatch):
    skill = _skill()
    navigator = _Navigator()
    skill.__dict__["navigator"] = navigator

    def planner(*args, check_cancelled, **kwargs):
        skill.nav_mode = NavMode("mapping")
        check_cancelled()
        return [_goal()]

    monkeypatch.setattr(explore_module, "find_frontier_goals", planner)

    with pytest.raises(SkillCancelled, match="mode changed"):
        skill.execute(max_frontiers=1)
    assert navigator.calls == []
