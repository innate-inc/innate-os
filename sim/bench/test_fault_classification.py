"""Setup is ours; the run is the agent's -- and the boundary is a place in the
code, not an exception type.

Classifying by a `HarnessFault` type let an agent be excused by raising that
type from its own act loop, while a real setup failure -- world construction,
the judge, the nav map -- raised an ordinary exception and was charged to the
agent. `_prepare` is now the boundary: everything it does is the harness's, so
a failure there blocks the episode; everything after is the run, so a crash
there is a failed challenge, finalised with the measurements actually taken.

These inject at the real call sites rather than at the classification, because
the classification is exactly what is under test.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))

from runner import run_episode

MAP, CID = "gallery", "gallery_ring_tour"


def oracle_for(ch):
    from oracles import plan_for
    from planner_agent import PlannerAgent

    return PlannerAgent(plan_for(ch))


# --- setup: ours ----------------------------------------------------------


@pytest.mark.parametrize("target", ["mars_sim_driver.core.VirtualMars", "mars_sim_driver.challenges.ChallengeEngine"])
def test_a_failure_building_the_world_or_judge_is_blocked(monkeypatch, target):
    mod, name = target.rsplit(".", 1)
    __import__(mod)

    def boom(*a, **k):
        raise RuntimeError(f"{name} exploded")

    monkeypatch.setattr(sys.modules[mod], name, boom)
    e = run_episode(MAP, CID, oracle_for, agent_name="oracle")
    assert e.blocked.startswith("harness:"), f"{name} failure charged to the robot: {e.blocked!r}"
    assert name in e.error


def test_a_failure_building_the_agent_is_blocked():
    def boom(ch):
        raise RuntimeError("no API key")

    e = run_episode(MAP, CID, boom, agent_name="brain:x")
    assert e.blocked.startswith("harness:"), e.blocked
    assert "no API key" in e.error
    assert e.agent == "brain:x"


def test_a_failure_building_the_nav_map_is_blocked(monkeypatch):
    import navplan

    monkeypatch.setattr(
        navplan.NavMap,
        "from_sim",
        staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nav grid exploded"))),
    )
    e = run_episode(MAP, CID, oracle_for, agent_name="oracle")
    assert e.blocked.startswith("harness:"), e.blocked
    assert "nav grid exploded" in e.error


def test_an_exception_from_start_is_blocked_like_a_refusal(monkeypatch):
    from mars_sim_driver.challenges import ChallengeEngine

    monkeypatch.setattr(
        ChallengeEngine, "start", lambda self, cid: (_ for _ in ()).throw(RuntimeError("judge exploded"))
    )
    e = run_episode(MAP, CID, oracle_for, agent_name="oracle")
    assert e.blocked.startswith("harness:"), e.blocked


def test_a_missing_challenge_is_blocked_and_keeps_the_agent_name():
    e = run_episode(MAP, "definitely_not_a_challenge", lambda ch: None, agent_name="brain:x")
    assert e.blocked.startswith("harness:"), e.blocked
    assert e.agent == "brain:x"


# --- the run: the agent's -------------------------------------------------


class _Crashing:
    """An agent that acts for a while and then falls over."""

    name = "crasher"
    done = False

    def __init__(self, inner, after=0, exc=RuntimeError):
        self._inner, self._after, self._exc, self._n = inner, after, exc, 0

    def reset(self, *a, **k):
        return self._inner.reset(*a, **k)

    def act(self, *a, **k):
        self._n += 1
        if self._n > self._after:
            raise self._exc("agent act crashed")
        return self._inner.act(*a, **k)


def test_a_crash_during_the_run_is_the_agents_failure():
    e = run_episode(MAP, CID, lambda ch: _Crashing(oracle_for(ch)), agent_name="oracle")
    assert e.blocked == "", f"an agent crash was excused as a harness fault: {e.blocked!r}"
    assert not e.passed
    assert "act crashed" in e.error


def test_a_crash_keeps_the_measurements_it_really_took():
    """Rebuilding the episode from zeros would invent goal totals and times."""
    e = run_episode(MAP, CID, lambda ch: _Crashing(oracle_for(ch), after=4000), agent_name="oracle")
    assert e.blocked == ""
    assert e.goals_total > 0, "the challenge's goal count was fabricated as zero"
    assert e.steps > 0 and e.elapsed_s > 0, f"invented zero measurements: {e}"


def test_a_crash_in_agent_reset_is_still_the_agents():
    class BadReset:
        name = "bad"
        done = False

        def reset(self, *a, **k):
            raise RuntimeError("reset exploded")

        def act(self, *a, **k):
            raise AssertionError("unreachable")

    e = run_episode(MAP, CID, lambda ch: BadReset(), agent_name="oracle")
    assert e.blocked == "", f"a post-start crash was excused: {e.blocked!r}"
    assert "reset exploded" in e.error


def test_a_keyboardinterrupt_from_the_agent_is_that_agents_failure(monkeypatch):
    """Inside a worker an interrupt can only have come from the code under
    test -- workers install their own SIGINT handler. Letting it escape kills
    the worker, and the parent can only see a timeout, which then marks
    unrelated queued episodes blocked."""
    import multiprocessing as mp

    # What makes a process a worker is having a PARENT, which nothing inside it
    # can change. current_process().name is an ordinary mutable attribute.
    monkeypatch.setattr(mp, "parent_process", lambda: object())
    e = run_episode(MAP, CID, lambda ch: _Crashing(oracle_for(ch), exc=KeyboardInterrupt), agent_name="oracle")
    assert e.blocked == "", e.blocked
    assert "KeyboardInterrupt" in e.error


def test_renaming_the_process_cannot_win_an_interrupt(monkeypatch):
    """The old check read current_process().name, which code in the worker can
    set to "MainProcess" -- so an agent could get its interrupt re-raised and
    kill the worker on purpose."""
    import multiprocessing as mp

    monkeypatch.setattr(mp, "parent_process", lambda: object())
    monkeypatch.setattr(mp.current_process(), "name", "MainProcess", raising=False)
    e = run_episode(MAP, CID, lambda ch: _Crashing(oracle_for(ch), exc=KeyboardInterrupt), agent_name="oracle")
    assert e.blocked == "", "a renamed process talked its way into killing the worker"


def test_a_real_ctrl_c_in_the_root_process_still_propagates():
    """In the root process an interrupt is the user, and not a result."""
    with pytest.raises(KeyboardInterrupt):
        run_episode(MAP, CID, lambda ch: _Crashing(oracle_for(ch), exc=KeyboardInterrupt), agent_name="oracle")


def test_a_ctrl_c_during_setup_also_propagates():
    """Setup is often the expensive part; an interrupt there is control flow,
    not a blocked episode."""

    def boom(ch):
        raise KeyboardInterrupt("ctrl-c during setup")

    with pytest.raises(KeyboardInterrupt):
        run_episode(MAP, CID, boom, agent_name="oracle")


def test_a_failing_metrics_call_does_not_discard_the_episode(monkeypatch):
    """Finalisation is part of the guarded run: a metrics() that raises after a
    crash must not throw away the crash and the measurements with it."""
    from mars_sim_driver.challenges import ChallengeEngine

    monkeypatch.setattr(ChallengeEngine, "metrics", lambda self: (_ for _ in ()).throw(RuntimeError("metrics failed")))
    e = run_episode(MAP, CID, lambda ch: _Crashing(oracle_for(ch), after=200), agent_name="oracle")
    assert e.blocked == "", e.blocked
    assert "act crashed" in e.error, f"the original crash was lost: {e.error}"
    assert "metrics failed" in e.error, f"the finalisation failure was hidden: {e.error}"
    assert e.goals_total > 0 and e.steps > 0, f"measurements were fabricated: {e}"


def test_an_agent_property_that_raises_does_not_escape():
    """`turns` is already a property and `blocked_reason` could be; a raise
    from one used to escape run_episode entirely."""

    class BadProp:
        name = "bad"
        done = True

        def reset(self, *a, **k):
            pass

        def act(self, *a, **k):
            pass

        @property
        def blocked_reason(self):
            raise RuntimeError("property exploded")

    e = run_episode(MAP, CID, lambda ch: BadProp(), agent_name="oracle")
    assert "property exploded" in e.error
    assert e.goals_total > 0, "the episode was discarded by a bad property"


def test_a_healthy_episode_is_untouched():
    e = run_episode(MAP, CID, oracle_for, agent_name="oracle")
    assert e.blocked == "" and e.error == ""
    assert e.passed, e.reason
