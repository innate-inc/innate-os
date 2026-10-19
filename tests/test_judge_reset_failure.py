# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""A judge that cannot clear its own state must not score the robot.

Predicates carry state between episodes, so `start()` resets each one. If a
reset raises, the predicate may still hold the LAST episode's state and
anything scored afterwards is measuring the previous run. The engine refuses
to start rather than produce a number from a dirty instrument -- and the
refusal has to reach the report as OUR fault, because the whole point of this
benchmark is telling a broken harness apart from a robot that cannot do the
task.
"""

import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PACKAGE = REPO_ROOT / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"

try:
    import mujoco  # noqa: F401
except ImportError:
    fake = types.ModuleType("mujoco")
    fake.MjModel = object
    fake.MjSpec = object
    sys.modules["mujoco"] = fake

sys.path.insert(0, str(DRIVER_PACKAGE))

from mars_sim_driver.challenges import (  # noqa: E402
    Challenge,
    ChallengeEngine,
    Goal,
)


class Dirty:
    """A predicate whose reset raises -- a latch that will not clear."""

    def __init__(self):
        self.resets = 0

    def reset(self):
        self.resets += 1
        raise RuntimeError("dirty latch")

    def __call__(self, *a, **k):
        raise AssertionError("a predicate was judged after its reset failed")


class Clean:
    def reset(self):
        pass

    def __call__(self, *a, **k):
        raise AssertionError("a predicate was judged after a sibling's reset failed")


def _engine(tmp_path, challenge):
    sim = SimpleNamespace(
        data=SimpleNamespace(time=0.0),
        drop_prop_at=lambda *a, **k: True,
        reset=lambda *a, **k: None,
    )
    engine = ChallengeEngine(sim, threading.Lock(), roots=[], progress_path=tmp_path / "p.json")
    engine.challenges[challenge.id] = challenge
    return engine, sim


def _challenge(predicate, fail_if=None):
    return Challenge(
        id="dirty",
        title="Dirty",
        brief="brief",
        setup=[],
        goals=[Goal("first", predicate), Goal("second", Clean())],
        fail_if=fail_if,
    )


def test_a_goal_whose_reset_raises_refuses_to_start(tmp_path):
    bad = Dirty()
    engine, _sim = _engine(tmp_path, _challenge(bad))
    assert engine.start("dirty") is not True, "started on a judge that could not reset"
    assert engine.state == "failed"
    assert "judge error" in engine.reason and "reset" in engine.reason, engine.reason
    assert bad.resets == 1


def test_a_fail_if_whose_reset_raises_refuses_to_start(tmp_path):
    bad = Dirty()
    engine, _sim = _engine(tmp_path, _challenge(Clean(), fail_if=bad))
    assert engine.start("dirty") is not True
    assert engine.state == "failed"
    assert "judge error" in engine.reason, engine.reason


def test_the_refusal_does_not_leak_into_the_next_start(tmp_path):
    """_reset_failed is cleared, so a later healthy challenge still runs."""
    engine, _sim = _engine(tmp_path, _challenge(Dirty()))
    assert engine.start("dirty") is not True
    engine.challenges["ok"] = Challenge(id="ok", title="OK", brief="b", setup=[], goals=[Goal("g", Clean())])
    assert engine.start("ok"), "a healthy challenge was refused by the previous failure"
    assert engine.state == "running"
    assert engine.reason == ""


def test_a_healthy_challenge_starts(tmp_path):
    engine, _sim = _engine(tmp_path, _challenge(Clean()))
    assert engine.start("dirty")
    assert engine.state == "running"
