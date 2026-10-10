"""Simplified Python agents for the benchmark, per Innate's note that
VirtualMars can drive one directly instead of the full innate-os brain.

An agent is anything with act(mars, t) -> None called every control tick. It
drives the base with set_cmd_vel. CMD_VEL_TIMEOUT_S is 0.5, so velocity must
be RE-SENT every tick like a teleop publisher -- an agent that commands once
and then thinks stops moving.
"""

from __future__ import annotations

# Base limits, the same envelope planner_agent.py's follower drives inside.
V_MAX = 0.30
W_MAX = 1.2


class RandomAgent:
    """The validity gate. ARC-AGI-3 screens every environment against a random
    policy, because a task a random policy can pass measures nothing. This one
    re-rolls a velocity every `hold_s` so it actually explores rather than
    jittering in place -- a weaker random baseline would make the gate too easy
    to pass and hide a trivial challenge.
    """

    name = "random"

    def __init__(self, seed: int = 0, hold_s: float = 1.2):
        import random

        self.rng = random.Random(seed)
        self.hold_s = hold_s
        self._until = -1.0
        self._cmd = (0.0, 0.0)

    def reset(self, mars, challenge) -> None:
        self._until = -1.0

    def act(self, mars, t: float) -> None:
        if t >= self._until:
            self._cmd = (self.rng.uniform(-0.1, V_MAX), self.rng.uniform(-W_MAX, W_MAX))
            self._until = t + self.hold_s
        mars.set_cmd_vel(*self._cmd)
