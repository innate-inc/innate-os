"""Setup failures are ours; a crash during the run is the agent's.

Marking every escaping exception a harness fault removes the episode from the
score DENOMINATOR, so an agent that passes one challenge and crashes on the
next would be reported 1/1 instead of 1/2 -- the benchmark flattering the
system it exists to measure. The line is `engine.start`: before it the robot
was never asked anything, after it the run is the run.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))

import main
from runner import HarnessFault

JOB = ("gallery", "gallery_ring_tour", "oracle", 0, None)


def test_a_setup_failure_is_blocked(monkeypatch):
    def boom(*a, **k):
        raise HarnessFault("agent could not be built: RuntimeError: no API key")

    monkeypatch.setattr(main, "run_episode", boom)
    e = main._one(JOB)
    assert e.blocked.startswith("harness:"), e.blocked
    assert "no API key" in e.blocked
    assert not e.passed


def test_a_crash_during_the_run_is_the_agents_failure(monkeypatch):
    """Not blocked: excusing it would take the episode out of the denominator,
    so crashing would improve the agent's score."""

    def boom(*a, **k):
        raise RuntimeError("agent act crashed after engine.start")

    monkeypatch.setattr(main, "run_episode", boom)
    e = main._one(JOB)
    assert e.blocked == "", f"an agent crash was excused as a harness fault: {e.blocked}"
    assert not e.passed
    assert "act crashed" in e.error


def test_the_episode_keeps_the_agent_it_was_asked_for(monkeypatch):
    def boom(*a, **k):
        raise HarnessFault("nope")

    monkeypatch.setattr(main, "run_episode", boom)
    assert main._one(("gallery", "gallery_ring_tour", "brain:x", 0, None)).agent == "brain:x"


def test_a_missing_challenge_is_blocked_and_keeps_the_agent_name():
    from runner import run_episode

    e = run_episode("gallery", "definitely_not_a_challenge", lambda ch: None, agent_name="brain:x")
    assert e.blocked.startswith("harness:"), e.blocked
    assert e.agent == "brain:x", "the agent that was asked for was lost"


def test_a_judge_that_refuses_to_start_is_blocked_and_says_why(monkeypatch):
    """The judge knows why it refused; reporting a bare refusal turned a bug in
    the judge into a robot that could not do the task."""
    import runner
    from mars_sim_driver.challenges import ChallengeEngine

    real_start = ChallengeEngine.start

    def refuse(self, cid):
        real_start(self, cid)  # let it build the world, then refuse
        self.state = "failed"
        self.reason = "judge error: a predicate could not be reset"
        return False

    monkeypatch.setattr(ChallengeEngine, "start", refuse)
    e = runner.run_episode("gallery", "gallery_ring_tour",
                           lambda ch: _StubAgent(), agent_name="oracle")
    assert e.blocked.startswith("harness:"), e.blocked
    assert "predicate could not be reset" in e.blocked, e.blocked
    assert e.goals_total > 0, "the challenge's goal count was lost"


class _StubAgent:
    name = "stub"
    done = True

    def reset(self, *a, **k):
        raise AssertionError("the agent ran after the judge refused to start")

    def act(self, *a, **k):
        raise AssertionError("the agent acted after the judge refused to start")
