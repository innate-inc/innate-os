"""A harness fault must never be scored as a robot failure.

Episode.blocked is the benchmark's whole point in one field: the report has to
say "the robot failed this" and "we failed to ask it" as different sentences.
These pin the second one -- a blocked episode leaves the numerator AND the
denominator, it cannot certify or condemn a challenge, and its absence is
stated rather than silent.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bench_common import blocked_count, format_scorecard, gate_verdict, scorecard

VALID = {"a", "b"}
CATS = {"a": 2, "b": 2}
ORACLE_PASS = {"passed": True, "goals_done": 2, "goals_total": 2, "blocked": ""}


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


def rnd(*specs):
    return [{"passed": p, "blocked": b} for p, b in specs]


# --- the scorecard --------------------------------------------------------


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


def test_blocked_count_can_span_challenges_the_gate_rejected():
    """A blocked oracle makes its own challenge INCOMPLETE, so a VALID-scoped
    count reports zero for exactly the agent that lost everything."""
    rows = [ep("a", "oracle", False, "harness: worker died")]
    assert blocked_count(rows, "oracle", set()) == 0
    assert blocked_count(rows, "oracle") == 1


# --- the gate -------------------------------------------------------------


def test_a_blocked_oracle_is_incomplete_not_invalid():
    """Blaming the challenge for our crash would retire a good challenge."""
    dead = {"passed": False, "goals_done": 0, "goals_total": 2, "blocked": "harness: worker died"}
    verdict, why = gate_verdict("nav", dead, rnd((False, ""), (False, "")))
    assert verdict == "INCOMPLETE", (verdict, why)
    assert "oracle blocked" in why


def test_one_blocked_random_is_enough_to_be_incomplete():
    """The rule is that EVERY random rollout failed. With one missing we do
    not know that, and the survivors must not certify the challenge."""
    verdict, why = gate_verdict("nav", ORACLE_PASS, rnd((False, ""), (False, ""), (False, "harness: died")))
    assert verdict == "INCOMPLETE", (verdict, why)
    assert "1/3 random rollouts blocked" in why, why


def test_all_random_blocked_is_incomplete():
    verdict, _ = gate_verdict("nav", ORACLE_PASS, rnd((False, "harness: died"), (False, "harness: died")))
    assert verdict == "INCOMPLETE"


def test_intact_controls_still_certify():
    assert gate_verdict("nav", ORACLE_PASS, rnd((False, ""), (False, ""), (False, "")))[0] == "VALID"


def test_a_blocked_rollout_cannot_strengthen_the_control():
    """Counted as a failure, a blocked rollout would make chance look weaker
    and the challenge look better than the evidence supports."""
    passing = rnd((True, ""), (True, ""), (False, "harness: died"))
    assert gate_verdict("nav", ORACLE_PASS, passing)[0] == "INCOMPLETE"


def test_an_arm_challenge_with_blocked_controls_is_incomplete():
    verdict, _ = gate_verdict("arm", None, rnd((False, ""), (False, "harness: died")))
    assert verdict == "INCOMPLETE"


# --- the agent ------------------------------------------------------------


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
