# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for pure pose math and navigation-mode goal routing (no ROS)."""

import math

import pytest

from brain_client.brain.utils import adjust_nav_goal
from brain_client.perception.pose import (
    absolute_to_local_nav_command,
    adjust_local_nav_command,
    compute_pose_delta,
    local_to_absolute_nav_command,
)


def test_absolute_goal_ahead_becomes_forward_local_goal():
    # Robot at (1, 2) facing +x; goal 3m further along +x.
    out = absolute_to_local_nav_command({"x": 4.0, "y": 2.0, "theta_degrees": 0.0}, (1.0, 2.0, 0.0))
    assert out["local_frame"] is True
    assert out["x"] == pytest.approx(3.0)
    assert out["y"] == pytest.approx(0.0)
    assert out["theta_degrees"] == pytest.approx(0.0)


def test_absolute_heading_is_rebased_onto_robot_heading():
    # The demo-log case: robot at heading 22 deg, model wants absolute -158 deg
    # (turn around). Locally that is a 180 deg rotation in place.
    out = absolute_to_local_nav_command({"x": 0.0, "y": 0.0, "theta_degrees": -158.0}, (0.0, 0.0, math.radians(22.0)))
    assert out["local_frame"] is True
    assert out["x"] == pytest.approx(0.0)
    assert out["y"] == pytest.approx(0.0)
    assert abs(out["theta_degrees"]) == pytest.approx(180.0)


def test_absolute_goal_respects_rotated_robot_frame():
    # Robot at origin facing +y (90 deg); a goal at (0, 1) is 1m straight ahead.
    out = absolute_to_local_nav_command({"x": 0.0, "y": 1.0, "theta_degrees": 90.0}, (0.0, 0.0, math.radians(90.0)))
    assert out["x"] == pytest.approx(1.0)
    assert out["y"] == pytest.approx(0.0)
    assert out["theta_degrees"] == pytest.approx(0.0)


def test_absolute_conversion_preserves_radian_theta_key():
    out = absolute_to_local_nav_command({"x": 0.0, "y": 0.0, "theta": math.pi / 2}, (0.0, 0.0, 0.0))
    assert "theta_degrees" not in out
    assert out["theta"] == pytest.approx(math.pi / 2)


def test_local_frame_input_passes_through_unchanged():
    inputs = {"x": 1.0, "y": 0.0, "theta_degrees": 90.0, "local_frame": True}
    assert absolute_to_local_nav_command(inputs, (5.0, 5.0, 1.0)) is inputs


def test_absolute_conversion_does_not_mutate_input():
    inputs = {"x": 4.0, "y": 2.0, "theta_degrees": 0.0}
    absolute_to_local_nav_command(inputs, (1.0, 2.0, 0.0))
    assert inputs == {"x": 4.0, "y": 2.0, "theta_degrees": 0.0}


def test_rebase_then_capture_delta_compensation_compose():
    # A rebased goal is exact at the current pose: applying a zero capture
    # delta (robot has not moved since) must leave it unchanged.
    rebased = absolute_to_local_nav_command({"x": 2.0, "y": 0.0, "theta_degrees": 0.0}, (1.0, 0.0, 0.0))
    unchanged = adjust_local_nav_command(rebased, compute_pose_delta((1.0, 0.0, 0.0), (1.0, 0.0, 0.0)))
    assert unchanged["x"] == pytest.approx(rebased["x"])
    assert unchanged["y"] == pytest.approx(rebased["y"])
    assert unchanged["theta_degrees"] == pytest.approx(rebased["theta_degrees"])


def test_local_goal_becomes_absolute_in_rotated_map_pose():
    inputs = {"x": 2.0, "y": 1.0, "theta_degrees": 90.0, "local_frame": True}
    out = local_to_absolute_nav_command(inputs, (10.0, 20.0, math.pi / 2))

    assert out["local_frame"] is False
    assert out["x"] == pytest.approx(9.0)
    assert out["y"] == pytest.approx(22.0)
    assert out["theta_degrees"] == pytest.approx(180.0)
    assert inputs["local_frame"] is True


def test_local_to_absolute_preserves_radian_theta_key():
    out = local_to_absolute_nav_command(
        {"x": 0.0, "y": 0.0, "theta": math.pi / 2, "local_frame": True},
        (1.0, 2.0, math.pi / 2),
    )
    assert "theta_degrees" not in out
    assert abs(out["theta"]) == pytest.approx(math.pi)


def test_mapped_local_goal_uses_static_map_without_changing_world_target():
    # The robot moved 1m after the image: compensation leaves a 1m local goal,
    # then mapped-mode conversion restores the original x=2m world endpoint.
    out = adjust_nav_goal(
        {"x": 2.0, "y": 0.0, "theta_degrees": 0.0, "local_frame": True},
        capture_pose=(0.0, 0.0, 0.0),
        current_pose=(1.0, 0.0, 0.0),
        is_mapfree=False,
        use_static_map=True,
    )
    assert out["local_frame"] is False
    assert out["x"] == pytest.approx(2.0)
    assert out["y"] == pytest.approx(0.0)


def test_mapfree_local_goal_remains_local_after_capture_compensation():
    out = adjust_nav_goal(
        {"x": 2.0, "y": 0.0, "theta_degrees": 0.0, "local_frame": True},
        capture_pose=(0.0, 0.0, 0.0),
        current_pose=(1.0, 0.0, 0.0),
        is_mapfree=True,
    )
    assert out["local_frame"] is True
    assert out["x"] == pytest.approx(1.0)
    assert out["y"] == pytest.approx(0.0)


def test_mapfree_mode_ignores_retained_pose_for_local_goal():
    # The explicit current mode controls the conversion. A retained pose is
    # intentionally ignored for planner selection while mapfree is active.
    inputs = {"x": -0.4, "y": 0.0, "theta_degrees": 0.0, "local_frame": True}
    out = adjust_nav_goal(
        inputs,
        capture_pose=None,
        current_pose=(50.0, 50.0, 1.0),
        is_mapfree=True,
    )
    assert out is inputs


@pytest.mark.parametrize("mode", ["mapping", None])
def test_non_navigation_mode_keeps_local_goal_local(mode):
    inputs = {"x": 1.0, "y": 0.0, "theta_degrees": 0.0, "local_frame": True}
    out = adjust_nav_goal(
        inputs,
        capture_pose=(4.0, 5.0, 0.0),
        current_pose=(4.0, 5.0, 0.0),
        is_mapfree=mode == "mapfree",
        use_static_map=mode == "navigation",
    )
    assert out["local_frame"] is True
    assert out["x"] == pytest.approx(1.0)
    assert out["y"] == pytest.approx(0.0)


def test_navigation_mode_rejects_local_goal_without_map_pose():
    with pytest.raises(ValueError, match="current map pose is unavailable"):
        adjust_nav_goal(
            {"x": 1.0, "y": 0.0, "theta_degrees": 0.0, "local_frame": True},
            capture_pose=(4.0, 5.0, 0.0),
            current_pose=None,
            is_mapfree=False,
            use_static_map=True,
        )
