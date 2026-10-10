"""The last permitted decision is driven, not just charged.

`done` became true the moment the turn count reached max_turns, and act()
returned on `done` before stepping the primitive that decision had queued. So
an agent whose final turn was `forward` or `turn` paid for it and the robot
never moved; run_episode then ended the episode. A budget bounds how often the
model is asked, not whether the robot finishes what it was told.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import time
from types import SimpleNamespace

from backends import EchoBackend
from brain_agent import BrainAgent


class StraightLine:
    """A robot that drives exactly what it is told, along x."""

    def __init__(self) -> None:
        self.x = 0.0
        self.v = 0.0
        self.commands: list[tuple[float, float]] = []

    def pose(self) -> tuple[float, float, float]:
        return (self.x, 0.0, 0.0)

    def set_cmd_vel(self, v: float, w: float) -> None:
        self.v = v
        self.commands.append((v, w))

    def tick(self, dt: float) -> None:
        self.x += self.v * dt


def _run(agent: BrainAgent, mars: StraightLine, ticks: int = 600, dt: float = 0.1) -> None:
    t = 0.0
    for _ in range(ticks):
        agent.act(mars, t)
        mars.tick(dt)
        t += dt
        if agent.done:
            return
        time.sleep(0.002)  # the backend answers on a thread
    raise AssertionError("the agent never finished")


def test_the_final_forward_is_driven_to_the_end() -> None:
    agent = BrainAgent(EchoBackend([{"action": "forward", "args": '{"metres": 0.5}'}]), max_turns=1)
    mars = StraightLine()
    agent.reset(mars, SimpleNamespace(brief="drive"))
    _run(agent, mars)
    assert agent.turns == 1
    assert mars.x >= 0.47, f"the robot stopped at {mars.x:.2f} m of the 0.5 m it was told"
    assert mars.commands[-1] == (0.0, 0.0), "the primitive did not finish cleanly"


def test_no_further_model_call_after_the_budget() -> None:
    """The budget still binds: once the last primitive is done the agent is
    done, and the second script step is never asked for."""
    script = [{"action": "forward", "args": '{"metres": 0.2}'}, {"action": "turn", "args": '{"degrees": 90}'}]
    backend = EchoBackend(script)
    agent = BrainAgent(backend, max_turns=1)
    mars = StraightLine()
    agent.reset(mars, SimpleNamespace(brief="drive"))
    _run(agent, mars)
    assert agent.turns == 1
    assert backend.i == 1, "a second decision was requested after the budget was spent"
    assert all(w == 0.0 for _, w in mars.commands), "the turn from the unbudgeted second step was driven"
