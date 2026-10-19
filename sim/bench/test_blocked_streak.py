"""A blocked drive ends, and says so in Nav2's own words.

This file used to test Observation.blocked_streak, the harness's count of
blocked turn/forward primitives. Those primitives are gone -- the robot's brain
drives through navigate_to_position -- and so is the streak note, which the
brain never had. What carries the failure now is what carries it on the
robot: the skill's failed event, from Nav2's progress checker (0.5 m in 10 s,
mars_nav/config/controller.yaml).
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import innate_roster as R
from brain_agent import BrainAgent


class Wall:
    """A robot pressed against something: commands go out, it never moves."""

    def pose(self):
        return (0.0, 0.0, 0.0)

    def set_cmd_vel(self, v, w):
        pass


class Free:
    def __init__(self):
        self.x, self.v = 0.0, 0.0

    def pose(self):
        return (self.x, 0.0, 0.0)

    def set_cmd_vel(self, v, w):
        self.v = v


class Quiet:
    wants_image = False


def _drive(agent, mars, metres, seconds, dt=0.05, move=None):
    agent._dispatch(mars, 0.0, "navigate_to_position", {"x": metres, "y": 0.0, "local_frame": True})
    t = 0.0
    while agent._skill is not None and t < seconds:
        t += dt
        if move:
            move(dt)
        agent._step_skill(mars, t)
    return t


class Recorder(Wall):
    def __init__(self):
        self.commands = []

    def set_cmd_vel(self, v, w):
        self.commands.append(v)


def test_a_drive_into_a_wall_backs_off_twice_then_fails():
    """nav_to_pose.xml: on a stall Nav2 backs up 0.15 m and retries, twice."""
    agent = BrainAgent(Quiet())
    mars = Recorder()
    took = _drive(agent, mars, 2.0, 120.0)
    assert agent._skill is None
    stalls = (R.NAV_RECOVERIES + 1) * R.NAV_PROGRESS_S
    assert stalls <= took < stalls + 3.0
    assert any(v < 0 for v in mars.commands), "it never backed up"
    (event,) = agent._events
    assert event.startswith("Skill navigate_to_position failed: Navigation to (2.0, 0.0, 0.0 deg, local frame) failed")
    assert "Nav2 reported FAILED with 2.00m still to go after 2 recoveries" in event


def test_a_moving_drive_is_not_failed():
    agent = BrainAgent(Quiet())
    mars = Free()

    def move(dt):
        mars.x += mars.v * dt

    _drive(agent, mars, 2.0, 60.0, move=move)
    (event,) = agent._events
    assert event == "Skill navigate_to_position completed: Reached position (2.0, 0.0, 0.0 deg)"
    assert mars.x >= 2.0 - R.NAV_XY_TOL_M


class NoTurn(Free):
    """Drives, but a jamb stops it turning."""


def test_a_blocked_turn_at_the_goal_fails_instead_of_settling_forever():
    """blaze_l4: in the kitchen doorway the robot reached its goal point and
    could not turn to the goal heading; the skill ran four minutes with no
    event, because settling counted as progress whether or not it closed."""
    agent = BrainAgent(Quiet())
    mars = NoTurn()

    def move(dt):
        mars.x += mars.v * dt

    agent._dispatch(mars, 0.0, "navigate_to_position", {"x": 0.5, "y": 0.0, "theta_degrees": 90, "local_frame": True})
    t = 0.0
    while agent._skill is not None and t < 60.0:
        t += 0.05
        move(0.05)
        agent._step_skill(mars, t)
    assert t < 15.0, f"still settling after {t:.0f} s"
    (event,) = agent._events
    assert "short of its heading" in event
