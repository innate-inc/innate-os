#!/usr/bin/env python3
"""Which challenges this deployment can actually attempt.

WHY THIS EXISTS. `pick_any_object` fails on the first line of its `execute`
without an Innate proxy -- locating the object and verifying the grasp are
hosted vision calls, so no INNATE_SERVICE_KEY means no grasping at all. 22 of
the 45 challenges need a pick: 2 in category 1, 13 of the 17 in category 2, and
7 in category 3.

Running them anyway produces twenty-two confident zeros that look like an agent
that cannot follow instructions, when the truth is a capability that was never
wired up. A benchmark that cannot tell those apart is worse than one that
refuses to answer, so challenges whose goals require manipulation are reported
BLOCKED, with the reason, and left out of the score.

Requirements are DERIVED, not declared: a goal that constrains where a
non-robot object ends up can only be satisfied by carrying it there. Deriving
it means a challenge written next week is classified correctly without anyone
remembering to annotate it, and it cannot drift out of sync with the goals the
way a hand-maintained list would.

The derivation has two traps, both of which cost me a wrong answer before the
rule was right, and both of which are the reason this is a module with a test
rather than three lines inside the runner:

  * `Hold` is a duration wrapper, not a grasp (see below).
  * A placement the SETUP already satisfies is a stay-put goal, not a carry.

The per-prop reach check needs exactly the same "must this object move?"
judgement, and takes `needs_move` from here rather than keeping its own copy
-- two implementations of a rule this fiddly would eventually disagree, and
the one that disagreed silently would be the one shipping numbers.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import sys
from pathlib import Path

# Predicates that constrain a POSITION. The first field of each names the
# entity whose position is constrained -- Near(a, b), InCircle(target, ...),
# InRect(target, ...) -- and "robot" means drive there while anything else
# means carry it there.
#
# `Hold` is NOT in this list and must not be: despite the name it is a
# DURATION wrapper, Hold(inner=<predicate>, seconds=...), which says a
# condition has to stay true rather than that anything is being held. Reading
# it as a grasp wrongly blocks five category-1 challenges whose goals are
# Hold-wrapped stay-puts -- Hold(inner=InRect(target='robot', ...)), standing
# still in a doorway, or Hold(inner=Near(...)), holding position by a thing.
# _predicates() recurses into `inner`, so the wrapped predicate is judged on
# its own merits and the wrapper needs no special case at all.
_PLACEMENT_PREDICATES = ("InCircle", "InRect", "Near")

MANIPULATION = "pick_any_object"


def _predicates(node) -> list:
    """Every predicate in a goal tree, including inside AllOf/AnyOf/After."""
    found = []
    if dataclasses.is_dataclass(node):
        found.append(node)
        for field in dataclasses.fields(node):
            value = getattr(node, field.name)
            if dataclasses.is_dataclass(value):
                found += _predicates(value)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if dataclasses.is_dataclass(item):
                        found += _predicates(item)
    return found


def _dropped_at(challenge, prop: str) -> tuple[float, float] | None:
    """Where `prop` starts, if the challenge places it."""
    for drop in challenge.setup:
        if getattr(drop, "name", None) == prop:
            return float(drop.x), float(drop.y)
    return None


def _satisfied_at_setup(challenge, pred) -> bool:
    """Whether the initial layout already satisfies this placement.

    A goal the setup satisfies is a STAY-PUT goal -- "leave the teapot where it
    is", "the mug is untouched on the pass" -- and asks for no manipulation at
    all. counter_out_of_reach is the case that matters: its third goal is
    InCircle('counter_teapot', -2.10, -0.30, 0.30) over a Drop at exactly
    (-2.10, -0.30), so reading it as a carry blocks a challenge whose correct
    outcome is that nothing moved.
    """
    name = type(pred).__name__
    if name == "InCircle":
        here = _dropped_at(challenge, pred.target)
        return here is not None and math.hypot(pred.x - here[0], pred.y - here[1]) <= pred.radius_m
    if name == "InRect":
        here = _dropped_at(challenge, pred.target)
        return here is not None and (
            min(pred.x0, pred.x1) <= here[0] <= max(pred.x0, pred.x1)
            and min(pred.y0, pred.y1) <= here[1] <= max(pred.y0, pred.y1)
        )
    if name == "Near":
        a, b = _dropped_at(challenge, pred.a), _dropped_at(challenge, pred.b)
        return a is not None and b is not None and math.hypot(a[0] - b[0], a[1] - b[1]) <= pred.radius_m
    return False


def needs_move(challenge, prop: str | None = None) -> bool:
    """Does any goal require an object to END UP somewhere it does not start?

    With `prop`, asks about that object alone -- which is what a reach check wants,
    because reach only matters for things the robot has to pick up, and a ring
    of cans it must merely COUNT can sit anywhere. With no `prop`, asks about
    any object at all, which is what the capability gate wants.
    """
    for goal in challenge.goals:
        root = getattr(goal, "predicate", goal)
        for pred in _predicates(root):
            name = type(pred).__name__
            if name not in _PLACEMENT_PREDICATES:
                continue
            if name == "Near":
                # robot-to-prop is an APPROACH, not a pick. prop-to-prop means
                # one of them has to be carried to the other, and either end
                # counts: the thing picked up and the place it goes both have
                # to be within reach of somewhere the robot can stand.
                if "robot" in (pred.a, pred.b):
                    continue
                subjects = (pred.a, pred.b)
            else:
                # InRect(target='robot', ...) is "stand in this doorway", which
                # is the whole of rounds_all_doors and half of category 3.
                # Dropping this check reads every navigation goal as a carry.
                if pred.target == "robot":
                    continue
                subjects = (pred.target,)
            if not all(isinstance(s, str) for s in subjects):
                continue
            if prop is not None and prop not in subjects:
                continue
            if _satisfied_at_setup(challenge, pred):
                continue
            return True
    return False


def needs_manipulation(challenge) -> bool:
    """Whether any goal can only be satisfied by picking something up.

    Two ways a challenge can demand it. Most say it by GEOMETRY -- an object
    has to end up somewhere it does not start -- which is `needs_move`. A few
    say it by NAME: `SkillDone("pick_any_object")`, a goal that is satisfied by
    the skill reporting success and by nothing else. A challenge whose only
    goal is exactly that reads, by geometry alone, as needing no pick.

    The name check lives here and NOT in `needs_move`, because `needs_move`
    also answers the per-prop reach question -- "must THIS object be within
    reach?" -- and a skill name is not a prop. Keeping it out preserves that
    function's behaviour exactly, which was verified equal to the reach check's
    previous private copy over every (challenge, prop) pair in the tree.
    """
    if needs_move(challenge):
        return True
    for goal in challenge.goals:
        for pred in _predicates(getattr(goal, "predicate", goal)):
            if type(pred).__name__ != "SkillDone":
                continue
            named = getattr(pred, "skill", "")
            named = [named] if isinstance(named, str) else list(named or [])
            # Skill ids are namespaced ("innate-os/pick_any_object"), so match
            # on the tail rather than equality.
            if any(str(s).split("/")[-1] == MANIPULATION for s in named):
                return True
    return False


def runtime_env() -> dict[str, str]:
    """What the ROBOT's environment is, not this process's.

    The gate must answer "can the stack grasp?", and the stack is a container
    configured from the repo `.env` (docker-compose mounts it as
    /root/innate-os/.env). The harness runs on the host, where those variables
    are usually absent -- so reading `os.environ` alone answers a different
    question and gets it wrong in the dangerous direction: with
    GEMINI_BASE_URL set in `.env` and unset in the shell, every run would
    silently drop 22 of 45 challenges from the score as "blocked" while the
    robot was perfectly able to pick things up.

    `scripts/print_runtime_env.build_runtime_env` is the repo's own resolver
    (/etc/innate.env, then `.env` on top), so this uses that rather than a
    second parser that could disagree with it. The process environment layers
    on top, so an explicit `GEMINI_BASE_URL=... live_runner.py` still wins.
    """
    merged: dict[str, str] = {}
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
        from print_runtime_env import build_runtime_env  # noqa: PLC0415

        merged.update(build_runtime_env(Path(__file__).resolve().parents[2]))
    except Exception:  # noqa: BLE001 -- fall back to the process env alone
        pass
    merged.update({k: v for k, v in os.environ.items() if v})
    return merged


def missing_capabilities(env: dict[str, str] | None = None) -> set[str]:
    """Capabilities this deployment cannot perform, from the environment.

    Grasping needs a VISION backend, not specifically an Innate key. The chain
    is `pick_any_object._proxy` -> `innate.gemini.make_client()`, which returns
    a ProxyClient when `INNATE_SERVICE_KEY` is set (proxy_url already defaults
    to Innate's), OTHERWISE a `_DirectClient` when `GEMINI_BASE_URL` is set,
    and only `None` when neither is. `execute()` fails on `None`.

    Innate's own docstring on `_DirectClient` says GEMINI_BASE_URL exists for
    exactly this case: "a dev setup with no service key gets a working brain
    and skills that still fail with 'Innate proxy not configured'.
    GEMINI_BASE_URL now covers both."

    Gating on the key alone would block twenty-two challenges that a base URL
    would have run.
    """
    env = env if env is not None else runtime_env()
    missing = set()
    if not (env.get("INNATE_SERVICE_KEY", "").strip() or env.get("GEMINI_BASE_URL", "").strip()):
        missing.add(MANIPULATION)
    return missing


def _ungraspable() -> dict[str, str]:
    """Challenges whose target does not fit the gripper's aperture.

    Physical impossibility is a capability gap like any other, and belongs in
    the same place: reported BLOCKED, excluded from the score. Scoring a
    challenge 0 because the object I built is wider than the fingers attributes
    my prop dimensions to the robot -- which is exactly what happened for every
    manipulation challenge in this suite until the aperture was measured.

    Read from a manifest rather than measured here: the measurement needs
    MuJoCo and a loaded world per map, far too heavy to run before every
    episode. The manifest is regenerated by the grasp-aperture check after
    props change; an empty one means every target fits.
    """
    path = Path(__file__).resolve().parent / "ungraspable.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def blocked_reason(challenge, env: dict[str, str] | None = None) -> str | None:
    """Why this challenge cannot be attempted here, or None if it can."""
    if MANIPULATION in missing_capabilities(env) and needs_manipulation(challenge):
        return (
            "needs pick_any_object, which needs a grasp-vision backend: "
            "INNATE_SERVICE_KEY, or GEMINI_BASE_URL on an OpenAI-compatible endpoint"
        )
    too_wide = _ungraspable().get(getattr(challenge, "id", ""))
    if too_wide:
        return f"target does not fit the gripper: {too_wide}"
    return None
