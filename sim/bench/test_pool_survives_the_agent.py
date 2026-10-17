"""A worker must come back with a result, whatever the agent does to it.

multiprocessing.Pool does not notice a worker that dies, so a job lost this way
reaches the parent only as the result timeout -- and the recovery then marks
every unfinished episode blocked, which is one agent's crash contaminating
episodes that were merely queued behind it.

These run a real Pool rather than monkeypatching the classification, because
the classification is what is under test. Fork start method: the worker
inherits the patches made here.
"""

import multiprocessing as mp
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))

import main

JOB = ("gallery", "gallery_ring_tour", "oracle", 0, None)
pytestmark = pytest.mark.skipif(
    "fork" not in mp.get_all_start_methods(), reason="needs fork so the worker inherits the patch"
)


def through_a_pool(job=JOB, timeout=180):
    ctx = mp.get_context("fork")
    with ctx.Pool(1, maxtasksperchild=1, initializer=main._ignore_sigint) as pool:
        return pool.imap_unordered(main._one, [job]).next(timeout=timeout)


def test_an_agent_that_raises_keyboardinterrupt_still_returns_a_result(monkeypatch):
    from planner_agent import PlannerAgent

    def act(self, *a, **k):
        raise KeyboardInterrupt("from the agent")

    monkeypatch.setattr(PlannerAgent, "act", act)
    e = through_a_pool()
    assert e.blocked == "", f"one agent's interrupt was charged to the harness: {e.blocked!r}"
    assert "KeyboardInterrupt" in e.error
    assert e.goals_total > 0, "the episode was rebuilt from zeros"


def test_an_agent_cannot_rename_its_way_out(monkeypatch):
    """current_process().name is mutable; having a parent is not."""
    from planner_agent import PlannerAgent

    def act(self, *a, **k):
        mp.current_process().name = "MainProcess"
        raise KeyboardInterrupt("from the agent, wearing a disguise")

    monkeypatch.setattr(PlannerAgent, "act", act)
    e = through_a_pool()
    assert e.blocked == "", "a renamed worker talked its way into dying"
    assert "KeyboardInterrupt" in e.error


def test_an_agent_cannot_forge_the_multiprocessing_parent_marker(monkeypatch):
    """parent_process() reads a module global, so clearing it made the second
    version of this check believe the worker was the root process. The policy
    is now the caller's argument, which nothing in here can reach."""
    import multiprocessing.process as mpp

    from planner_agent import PlannerAgent

    def act(self, *a, **k):
        mpp._parent_process = None
        raise KeyboardInterrupt("from the agent, with the marker cleared")

    monkeypatch.setattr(PlannerAgent, "act", act)
    e = through_a_pool()
    assert e.blocked == "", "forged multiprocessing state killed the worker"
    assert "KeyboardInterrupt" in e.error


def test_an_agent_that_raises_systemexit_still_returns_a_result(monkeypatch):
    from planner_agent import PlannerAgent

    def act(self, *a, **k):
        raise SystemExit(3)

    monkeypatch.setattr(PlannerAgent, "act", act)
    e = through_a_pool()
    assert e.blocked == ""
    assert "SystemExit" in e.error


def test_a_healthy_episode_through_a_pool_is_unchanged():
    e = through_a_pool()
    assert e.passed, e.reason
    assert e.blocked == "" and e.error == ""


def test_workers_do_not_hand_their_signal_policy_to_child_processes():
    """SIG_IGN survives exec, so a model CLI launched by a worker would inherit
    it and outlive both the Ctrl-C and its parent, still spending on API calls.
    A caught handler resets to the default on exec."""
    import signal
    import subprocess

    ctx = mp.get_context("fork")
    with ctx.Pool(1, initializer=main._ignore_sigint) as pool:
        disposition = pool.apply(
            subprocess.check_output,
            ([sys.executable, "-c", "import signal;print(signal.getsignal(signal.SIGINT))"],),
            {"text": True},
        ).strip()
    # Measured: under SIG_IGN the child prints "1"; with a caught handler it
    # prints default_int_handler. Assert the latter, so a regression to
    # SIG_IGN (or to any other inherited disposition) fails here.
    assert "default_int_handler" in disposition, (
        f"a child of a worker inherited SIGINT disposition {disposition!r} instead of the "
        "default, so a model CLI would survive Ctrl-C and its parent as an orphan"
    )
    assert signal.getsignal(signal.SIGINT) is not signal.SIG_IGN, "the parent lost its own Ctrl-C"
