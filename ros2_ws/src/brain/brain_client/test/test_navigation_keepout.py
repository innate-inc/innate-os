# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Keep navigation safety and diagnostics intact across composed Nav2 workflows."""

import math
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_household_resident_roster as fixtures  # noqa: E402
import test_robot_state_qos as qos_fixtures  # noqa: E402

nav = fixtures.navigation_module


def _make_controller():
    navigator = fixtures._VisualApproachNavigator()
    controller = fixtures._visual_approach_controller(navigator)
    controller.skill.check_cancelled = lambda: None
    controller.skill.pose = SimpleNamespace(x=0.5, y=0.5, theta=0)
    controller.skill.odom = SimpleNamespace(x=0.5, y=0.5, theta=0)
    grid = np.zeros((7, 7), dtype=np.int8)
    keepout = np.zeros_like(grid)
    keepout[0, 4] = 100
    controller.skill.map = SimpleNamespace(
        grid=grid, keepout_grid=keepout, resolution=1, origin_x=0, origin_y=0, origin_theta=0, width=7, height=7
    )
    return controller, navigator


def test_aborted_planner_retains_classification_and_reports_unexecuted_approach():
    controller, navigator = _make_controller()
    checks = []

    def path_exists(_planner, pose):
        checks.append(pose.pose.position.x)
        if pose.pose.position.x >= 2:
            raise nav.NavigationPathIndeterminateError("planner aborted")
        return True

    controller._path_exists = path_exists
    with pytest.raises(nav.NavigationPathIndeterminateError) as caught:
        controller.go_to_position(4.5, 0.5, 0, False)
    assert isinstance(caught.value.__cause__, nav.NavigationPathIndeterminateError)
    assert len(checks) == 1 + nav.APPROACH_SEARCH_STEPS
    assert "keepout zone" in str(caught.value)
    assert "A reachable approach" in str(caught.value)
    assert "did not move there" in str(caught.value)
    assert navigator.goals == []
    assert controller._commanded_goal_pub.messages == []


def test_execute_rejects_keepout_with_safe_approach_without_dispatching_navigation():
    controller, navigator = _make_controller()
    skill = nav.NavigateToPosition(None)
    skill.map = controller.skill.map
    skill.pose = controller.skill.pose
    skill.odom = controller.skill.odom
    skill.controller = controller
    controller.skill = skill
    probes = []

    def optimistic_planner(_planner, pose):
        probes.append(pose.pose.position.x)
        return True

    controller._path_exists = optimistic_planner

    with pytest.raises(nav.SkillFailed) as caught:
        skill.execute(x=4.5, y=0.5, theta_degrees=90, local_frame=False)

    detail = str(caught.value)
    assert "navigation was refused because the target is inside a keepout zone" in detail
    assert "A reachable approach on the direct line is (3.88, 0.50)" in detail
    assert "The robot did not move there; navigate to that pose explicitly" in detail
    assert 0 < len(probes) <= nav.APPROACH_SEARCH_STEPS
    assert all(x < 4.0 for x in probes)
    assert navigator.goals == []
    assert controller._commanded_goal_pub.messages == []


def test_hint_planner_outage_preserves_original_error_and_stops_probing():
    controller, navigator = _make_controller()
    checks = []

    def path_exists(_planner, pose):
        checks.append(pose.pose.position.x)
        if len(checks) == 1:
            raise nav.NavigationPathIndeterminateError("original planner abort")
        raise nav.NavigationInfrastructureError("planner unavailable during hint")

    controller._path_exists = path_exists
    with pytest.raises(nav.NavigationPathIndeterminateError) as caught:
        controller.go_to_position(4.5, 0.5, 0, False)
    assert str(caught.value.__cause__) == "original planner abort"
    assert len(checks) == 2
    assert "A reachable approach" not in str(caught.value)
    assert "robot did not move" in str(caught.value)
    assert navigator.goals == []


def test_failed_execution_preserves_keepout_and_stopped_pose_diagnostics():
    controller, navigator = _make_controller()
    results = iter([None, nav.TaskResult.FAILED])
    controller._task_result = lambda: next(results)
    navigator.getFeedback = lambda: SimpleNamespace(
        distance_remaining=2.3,
        number_of_recoveries=2,
        current_pose=SimpleNamespace(
            pose=SimpleNamespace(position=SimpleNamespace(x=2.2, y=0.5)), header=SimpleNamespace(frame_id="map")
        ),
    )
    with pytest.raises(nav.NavigationExecutionError) as caught:
        controller.go_to_position(4.5, 0.5, 0, False)
    detail = str(caught.value)
    assert "2.30m still to go" in detail
    assert "stopped at (2.20, 0.50)" in detail
    assert "2 recovery attempts" in detail
    assert "keepout zone" in detail
    assert "target was not reached" in detail
    assert controller._active_goal_uuid is None


def test_rotated_map_goal_validation_keeps_keepout_and_unknown_checks():
    controller, _ = _make_controller()
    state = controller.skill.map
    state.origin_theta = math.pi / 2
    assert nav.map_goal_error(state, -0.5, 4.5) == "the target is inside a keepout zone"
    state.grid[0, 2] = -1
    assert "unknown" in nav.map_goal_error(state, -0.5, 2.5)
    assert nav.map_goal_error(state, -0.5, 1.5) is None


def test_keepout_stays_on_same_latched_feed_as_map_across_idle_restart():
    state_node = qos_fixtures._FeedNode()
    state_node.get_logger = lambda: SimpleNamespace()
    high_rate_node = qos_fixtures._FeedNode()
    provider = qos_fixtures.RobotStateProvider(
        state_node,
        None,
        manipulation=SimpleNamespace(node=high_rate_node, start=lambda: None, stop=lambda: None),
        mobility=None,
        head=None,
        memory=None,
        head_current_position_topic="/head/current_position",
    )
    provider.start_subscriptions()
    callback, qos = state_node.subscriptions["/nav/keepout_filter_mask"]
    assert qos.durability == qos_fixtures.QoSDurabilityPolicy.TRANSIENT_LOCAL
    assert "/nav/keepout_filter_mask" not in high_rate_node.subscriptions
    assert "/odom" in high_rate_node.subscriptions
    first = qos_fixtures.OccupancyGrid()
    callback(first)
    provider.stop_subscriptions()
    assert provider.last_keepout_map is first
    second = qos_fixtures.OccupancyGrid()
    callback(second)
    provider.start_subscriptions()
    assert provider.last_keepout_map is second
