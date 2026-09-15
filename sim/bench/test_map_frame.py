"""Prove local_to_absolute_nav_command is the exact inverse of its twin.

A sign error here does not crash. It sends the robot to a point mirrored about
its own heading -- plausible, on the map, and wrong -- and the only symptom is
a benchmark score that looks like an agent with poor spatial reasoning. Since
the stack cannot be run right now, the transform has to be checked against
something that is already trusted, and the trusted thing is
`absolute_to_local_nav_command`, which the shipped mapfree path has been using
all along.

So: for random robot poses and random goals, converting a map goal to local and
back must return the original. That pins rotation direction, translation order,
and angle wrapping at once, without a robot.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import math
import random

from brain_client.perception.pose import (
    absolute_to_local_nav_command,
    compute_pose_delta,
    local_to_absolute_nav_command,
)

TOL = 1e-9
FACING_NORTH = (0.0, 0.0, math.pi / 2)
ROBOT = (1.5, -2.0, 0.7)


def angles_equal(a: float, b: float) -> bool:
    return abs(math.atan2(math.sin(a - b), math.cos(a - b))) < 1e-9


def test_round_trip_over_2000_random_poses_and_goals() -> None:
    rng = random.Random(20260814)
    for _ in range(2000):
        robot = (rng.uniform(-5, 5), rng.uniform(-5, 5), rng.uniform(-math.pi, math.pi))
        goal = {"x": rng.uniform(-5, 5), "y": rng.uniform(-5, 5), "theta_degrees": rng.uniform(-180, 180)}
        local = absolute_to_local_nav_command(dict(goal), robot)
        back = local_to_absolute_nav_command(local, robot)
        assert abs(back["x"] - goal["x"]) < 1e-9
        assert abs(back["y"] - goal["y"]) < 1e-9
        assert angles_equal(math.radians(back["theta_degrees"]), math.radians(goal["theta_degrees"]))


def test_one_metre_ahead_of_a_north_facing_robot_is_0_1() -> None:
    # The convention itself, stated as a case a human can check by eye: robot
    # at the origin facing +y (90 deg), goal 1m "forward" and 0m lateral must
    # land at (0, 1) -- not (1, 0), which is what a missing rotation gives.
    out = local_to_absolute_nav_command({"x": 1.0, "y": 0.0, "theta_degrees": 0.0, "local_frame": True}, FACING_NORTH)
    assert abs(out["x"]) < TOL and abs(out["y"] - 1.0) < TOL


def test_one_metre_left_of_a_north_facing_robot_is_minus1_0() -> None:
    # Positive lateral is LEFT (compute_pose_delta's docstring). Facing north,
    # left is -x. A sign flip here mirrors every grounded goal.
    out = local_to_absolute_nav_command({"x": 0.0, "y": 1.0, "theta_degrees": 0.0, "local_frame": True}, FACING_NORTH)
    assert abs(out["x"] + 1.0) < TOL and abs(out["y"]) < TOL


def test_agrees_with_compute_pose_delta() -> None:
    # Agreement with the delta helper the rest of the brain uses.
    target = (3.0, 0.5, -1.2)
    fwd, lat, dth = compute_pose_delta(ROBOT, target)
    out = local_to_absolute_nav_command({"x": fwd, "y": lat, "theta": dth, "local_frame": True}, ROBOT)
    assert abs(out["x"] - target[0]) < 1e-9
    assert abs(out["y"] - target[1]) < 1e-9
    assert angles_equal(out["theta"], target[2])


def test_absolute_goals_pass_through_unchanged() -> None:
    # An absolute goal must pass through untouched, or a second conversion
    # would translate an already-converted goal a second time.
    absolute = {"x": 2.0, "y": 3.0, "theta_degrees": 45.0}
    assert local_to_absolute_nav_command(dict(absolute), ROBOT) == absolute


# The theta spelling must survive: writing back the other key would leave
# two conflicting headings in one dict.


def test_radians_in_radians_out() -> None:
    out = local_to_absolute_nav_command({"x": 1.0, "y": 0.0, "theta": 0.3, "local_frame": True}, ROBOT)
    assert "theta" in out and "theta_degrees" not in out


def test_degrees_in_degrees_out() -> None:
    out = local_to_absolute_nav_command({"x": 1.0, "y": 0.0, "theta_degrees": 30.0, "local_frame": True}, ROBOT)
    assert "theta_degrees" in out and "theta" not in out


def test_local_frame_is_cleared() -> None:
    out = local_to_absolute_nav_command({"x": 1.0, "y": 0.0, "theta_degrees": 30.0, "local_frame": True}, ROBOT)
    assert out.get("local_frame") is False
