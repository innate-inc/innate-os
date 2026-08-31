"""Observation.blocked_streak is the harness's own count of consecutive
blocked primitives, and only a completed FORWARD resets it.

A completed turn proves the robot can rotate in place, not that the space
ahead is clear; an earlier version that reset on any completed primitive was
inert on the exact stuck-loop this exists to catch (blocked forward ->
successful recovery turn -> blocked forward, FINDINGS.md T18).
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import math

import pytest
from brain_agent import PRIMITIVE_TIMEOUT_S, BrainAgent, Observation


class FakeMars:
    def __init__(self, pose=(0.0, 0.0, 0.0)):
        self._pose = pose

    def pose(self):
        return self._pose

    def set_cmd_vel(self, v, w):
        pass


class FakeBackend:
    wants_image = False
    wants_pose = False


class FakeChallenge:
    brief = "test"


@pytest.fixture
def agent() -> BrainAgent:
    return BrainAgent(FakeBackend())


@pytest.fixture
def mars() -> FakeMars:
    return FakeMars()


def blocked_forward(agent, mars, start_pose, moved_frac_target, target_m=1.5):
    """Simulate a forward primitive that times out having covered only a
    small fraction of its target -- matches the traced 'gave up ... probably
    blocked' pattern."""
    agent._prim = ("forward", target_m, 0.0, start_pose)
    x, y, yaw = start_pose
    mars._pose = (x + moved_frac_target * target_m, y, yaw)
    return agent._step_primitive(mars, PRIMITIVE_TIMEOUT_S + 0.1)


def completed_turn(agent, mars, start_pose, degrees):
    """Simulate a turn primitive that actually reaches its target heading."""
    agent._prim = ("turn", math.radians(degrees), 0.0, start_pose)
    x, y, yaw = start_pose
    mars._pose = (x, y, yaw + math.radians(degrees))
    return agent._step_primitive(mars, 1.0)


def completed_forward(agent, mars, start_pose, metres):
    agent._prim = ("forward", metres, 0.0, start_pose)
    x, y, yaw = start_pose
    mars._pose = (x + metres, y, yaw)
    return agent._step_primitive(mars, 1.0)


# 1. Basic increment on a blocked forward.


def test_blocked_streak_increments_on_a_timed_out_forward(agent, mars) -> None:
    blocked_forward(agent, mars, (0.0, 0.0, 0.0), 0.05)
    assert agent._blocked_streak == 1


# 2. THE MOTIVATING SCENARIO, replayed directly: blocked forward, a
# SUCCESSFUL recovery turn, blocked forward again -- this is the exact
# pattern a traced episode showed (see FINDINGS.md T18) and is what an
# earlier version of this fix was inert against, because resetting on ANY
# completed primitive (including the turn) wiped the count every cycle
# and it could never climb past 1. This is the one test that would have
# caught that before it shipped, not after.


def test_a_completed_turn_does_not_reset_the_streak(agent, mars) -> None:
    blocked_forward(agent, mars, (0.0, 0.0, math.radians(74)), 0.26)
    assert agent._blocked_streak == 1
    completed_turn(agent, mars, mars._pose, 75)
    assert agent._blocked_streak == 1


def test_streak_climbs_across_intervening_successful_turns(agent, mars) -> None:
    blocked_forward(agent, mars, (0.0, 0.0, math.radians(74)), 0.26)
    completed_turn(agent, mars, mars._pose, 75)
    blocked_forward(agent, mars, mars._pose, 0.26)
    assert agent._blocked_streak == 2
    completed_turn(agent, mars, mars._pose, 75)
    blocked_forward(agent, mars, mars._pose, 0.26)
    assert agent._blocked_streak == 3  # matching the traced episode


# 3. Only a completed FORWARD resets it -- proves the path was actually clear.


def test_a_completed_forward_resets_the_streak_to_zero(agent, mars) -> None:
    blocked_forward(agent, mars, (0.0, 0.0, 0.0), 0.26)
    blocked_forward(agent, mars, mars._pose, 0.26)
    assert agent._blocked_streak == 2
    completed_forward(agent, mars, mars._pose, 1.5)
    assert agent._blocked_streak == 0


# 4. A blocked TURN also counts toward the streak (both primitive kinds can
# discover the robot cannot move the way it just tried to).


def test_a_timed_out_turn_also_increments_the_streak(agent, mars) -> None:
    agent._prim = ("turn", math.radians(90), 0.0, (0.0, 0.0, 0.0))
    mars._pose = (0.0, 0.0, math.radians(2))  # barely turned
    agent._step_primitive(mars, PRIMITIVE_TIMEOUT_S + 0.1)
    assert agent._blocked_streak == 1


# 5. Non-movement actions go through the REAL _apply() dispatch (not a
# hand-simulated result) for the actions that need no mars interaction --
# look/say/answer/finish/unknown -- and must leave the streak untouched.
# Static analysis (grep) shows _apply() never references _blocked_streak
# anywhere, but this exercises that property through the actual code path
# rather than trusting the grep alone.


@pytest.mark.parametrize("action", ["look", "say", "answer", "finish", "totally_unknown_action"])
def test_apply_non_movement_action_leaves_the_streak_untouched(agent, action) -> None:
    agent._blocked_streak = 2
    agent._apply(None, 0.0, {"action": action, "args": {}})
    assert agent._blocked_streak == 2


# 6. reset() clears the streak across episodes.


def test_reset_clears_blocked_streak(agent, mars) -> None:
    agent._blocked_streak = 3
    agent.reset(mars, FakeChallenge())
    assert agent._blocked_streak == 0


# 7. End-to-end wiring: _observe() itself (not a hand-built Observation)
# carries the real _blocked_streak into the Observation it returns, and
# the warning threshold/content is driven by that real value.


def test_observe_carries_the_real_blocked_streak_through(agent, mars) -> None:
    agent._blocked_streak = 3
    assert agent._observe(mars, 10.0).blocked_streak == 3


def test_the_resulting_observation_text_includes_the_warning(agent, mars) -> None:
    agent._blocked_streak = 3
    text = agent._observe(mars, 10.0).as_text()
    assert "NOTE:" in text and "3 times" in text


def test_observe_at_streak_one_produces_no_warning(agent, mars) -> None:
    agent._blocked_streak = 1
    assert "NOTE:" not in agent._observe(mars, 10.0).as_text()


# 8. Warning text does not assume "Last action" is the blocked one (it may
# not be, if a non-movement action happened since) and does not prescribe
# actions unavailable to a blind backend (no "look" recommendation baked
# into shared text).

WARN_TEXT = Observation(brief="x", elapsed_s=0.0, blocked_streak=3).as_text()


def test_warning_does_not_hardcode_an_ordinal() -> None:
    assert "4th" not in WARN_TEXT


def test_warning_does_not_assume_last_action_is_the_blocked_one() -> None:
    assert "see 'Last action'" not in WARN_TEXT


def test_warning_does_not_prescribe_look() -> None:
    assert "look first" not in WARN_TEXT.lower()
