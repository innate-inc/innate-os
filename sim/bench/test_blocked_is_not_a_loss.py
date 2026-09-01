"""A harness fault must never be scored as a robot failure.

Episode.blocked is the benchmark's whole point in one field: the report has to
be able to say "the robot failed this" and "we failed to ask it" as different
sentences. These pin the second one -- a blocked episode leaves the numerator
AND the denominator, and its absence is stated rather than silent.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_common import blocked_count, format_scorecard, scorecard

VALID = {"a", "b"}
CATS = {"a": 2, "b": 2}


def ep(challenge, agent, passed, blocked=""):
    return {
        "challenge": challenge,
        "agent": agent,
        "passed": passed,
        "blocked": blocked,
        "goals_done": 2 if passed else 0,
        "goals_total": 2,
        "elapsed_s": 10.0,
        "turns": 5,
        "path_len_m": 3.0,
    }


def test_blocked_episode_is_not_a_loss():
    rows = [ep("a", "x", True), ep("b", "x", False, "harness: backend error: RuntimeError")]
    _, total = scorecard(rows, CATS, VALID, "x")
    assert total[:2] == (1, 1), f"blocked episode still scored: {total}"
    assert total[2:] == (2, 2), "a blocked episode contributed goals"


def test_blocked_is_counted_and_stated():
    rows = [ep("a", "x", True), ep("b", "x", False, "harness: backend error: X")]
    assert blocked_count(rows, "x", VALID) == 1
    lines = format_scorecard(*scorecard(rows, CATS, VALID, "x"), 1)
    assert any("not scored" in ln and "not the robot" in ln for ln in lines), lines


def test_unblocked_failure_still_counts_against_the_robot():
    rows = [ep("a", "x", True), ep("b", "x", False)]
    _, total = scorecard(rows, CATS, VALID, "x")
    assert total[:2] == (1, 2), f"a real failure was excused: {total}"
    assert blocked_count(rows, "x", VALID) == 0
    assert not any("not scored" in ln for ln in format_scorecard(*scorecard(rows, CATS, VALID, "x"), 0))


def test_backend_error_marks_the_agent_blocked():
    from brain_agent import BrainAgent

    class Boom:
        wants_image = False
        think_charge_s = 0.0

        def decide(self, obs, menu):
            raise RuntimeError("credits depleted")

    a = BrainAgent(Boom(), max_turns=3)
    a._call_started = 0.0
    a._apply(None, 0.0, {"action": "_error", "args": {"detail": "RuntimeError: credits depleted"}})
    assert a.blocked_reason.startswith("harness:"), a.blocked_reason
    assert "credits depleted" in a.blocked_reason


def test_a_working_agent_is_never_blocked():
    from brain_agent import BrainAgent

    class Fine:
        wants_image = False
        think_charge_s = 0.0

        def decide(self, obs, menu):
            return {"action": "finish", "args": {}}

    assert BrainAgent(Fine(), max_turns=3).blocked_reason == ""
