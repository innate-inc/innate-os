"""Derive an oracle plan from a Challenge's own goals.

Hand-authoring waypoints does not scale past a dozen challenges, and there are
90 to gate. But a goal already states what has to be true, in world
coordinates: Near("robot", "sock", 0.5) says stand next to the sock;
InRect("robot", ...) says be in this box; InCircle("mug", x, y, r) says put the
mug there. That is enough to build a plan mechanically.

Two things this deliberately does NOT do:

  * It does not attempt SkillDone. Those goals need the arm and the brain's
    skill events, neither of which a scripted base agent has. Such challenges
    are classified requires_arm and gated on the weaker rule -- see classify().
  * It does not guess at AnyOf. Satisfying one branch is enough, so it plans
    for the first branch it can, and records that the plan covers a subset.

An auto-plan failing therefore means one of three things, and the runner keeps
them apart: the goal is unreachable (no path), the plan ran out of time, or the
challenge needs capabilities this agent does not have.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"))

from mars_sim_driver.challenges import (  # noqa: E402
    After,
    AllOf,
    Answered,
    AnyOf,
    Challenge,
    Hold,
    InCircle,
    InRect,
    Near,
    Predicate,
    Said,
    SkillDone,
)

# What an agent needs to satisfy a goal, worst case over its predicates.
NAV_ONLY = "nav"  # drive somewhere
CARRY = "carry"  # move a prop somewhere (abstracted; no arm)
NEEDS_ARM = "arm"  # a skill completion: real manipulation
UNKNOWN = "unknown"  # a predicate shape this planner does not model


def _unwrap(p: Predicate) -> tuple[Predicate, float]:
    """Strip a Hold or an After, returning (inner, dwell_seconds).

    An After is stripped with NO dwell. The oracle exists to witness that a
    goal is satisfiable, and a goal gated on the clock is satisfiable by
    waiting -- which proves nothing about the geometry and would cost the gate
    a minute of sim time per challenge. An After inside fail_if is never
    planned for at all: the oracle takes the safe route rather than
    demonstrating the trap.
    """
    if isinstance(p, Hold):
        return (p.inner, p.seconds)
    if isinstance(p, After):
        return (p.inner, 0.0)
    return (p, 0.0)


def requirement(p: Predicate) -> str:
    p, _ = _unwrap(p)
    if isinstance(p, SkillDone):
        return NEEDS_ARM
    if isinstance(p, (AllOf, AnyOf)):
        reqs = [requirement(c) for c in p.preds]
        for level in (NEEDS_ARM, UNKNOWN, CARRY, NAV_ONLY):
            if level in reqs:
                return level
        return NAV_ONLY
    if isinstance(p, Answered):
        return NAV_ONLY  # nothing has to move; the oracle just reports
    if isinstance(p, Said):
        # A negated Said ("never claimed it did something it did not do") is
        # satisfied by silence, so the oracle has nothing to do for it either.
        return NAV_ONLY
    if isinstance(p, Near):
        if p.a == "robot" or p.b == "robot":
            return NAV_ONLY
        return CARRY  # prop-to-prop: something has to move
    if isinstance(p, (InCircle, InRect)):
        return NAV_ONLY if p.target == "robot" else CARRY
    return UNKNOWN


def classify(ch: Challenge) -> str:
    reqs = [requirement(g.predicate) for g in ch.goals]
    for level in (NEEDS_ARM, UNKNOWN, CARRY, NAV_ONLY):
        if level in reqs:
            return level
    return NAV_ONLY


def _centre(p) -> tuple[float, float]:
    if isinstance(p, InCircle):
        return (p.x, p.y)
    return ((p.x0 + p.x1) / 2.0, (p.y0 + p.y1) / 2.0)


def steps_for_goal(p: Predicate) -> list[tuple] | None:
    """Steps that make one goal true, or None if this planner cannot."""
    p, dwell = _unwrap(p)
    tail: list[tuple] = [("wait", dwell + 0.5)] if dwell else []

    if isinstance(p, AllOf):
        out: list[tuple] = []
        for child in p.preds:
            s = steps_for_goal(child)
            if s is None:
                return None
            out += s
        return out + tail
    if isinstance(p, AnyOf):
        for child in p.preds:
            s = steps_for_goal(child)
            if s is not None:
                return s + tail
        return None

    if isinstance(p, Answered):
        # The first accepted spelling is by convention the canonical one.
        return [("answer", p.accept[0])] + tail

    if isinstance(p, Said):
        if p.negate:
            return tail  # satisfied by not saying it; nothing to schedule
        if not p.oracle_line:
            return None  # unauthored: refuse to gate rather than guess
        return [("say", p.oracle_line)] + tail

    if isinstance(p, Near):
        if p.a == "robot":
            return [("near", p.b)] + tail
        if p.b == "robot":
            return [("near", p.a)] + tail
        # Prop to prop: fetch the first and set it down beside the second.
        return [("near", p.a), ("grab", p.a), ("near", p.b), ("put_near", p.a, p.b)] + tail

    if isinstance(p, (InCircle, InRect)):
        cx, cy = _centre(p)
        if p.target == "robot":
            return [("goto", cx, cy)] + tail
        return [("near", p.target), ("grab", p.target), ("goto", cx, cy), ("put", p.target, cx, cy)] + tail

    return None  # SkillDone and anything unmodelled


def plan_for(ch: Challenge) -> list[tuple] | None:
    """A plan for the whole challenge, or None if any goal is unplannable.

    Goals are strictly ordered and latching, so the plan is just their steps in
    order -- and that ordering is exactly why a plan can be built at all.
    """
    out: list[tuple] = []
    for g in ch.goals:
        s = steps_for_goal(g.predicate)
        if s is None:
            return None
        out += s
    return out
