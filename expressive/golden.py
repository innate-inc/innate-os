"""The golden fixture: recipes run through every pure stage, for verifying ports (the studio's JS).

``uv run mars-express golden`` rewrites ``fixtures/golden.json``; rerun it whenever dsl, plan,
liveliness or basis.json change.
"""

from __future__ import annotations

import json
from pathlib import Path

import _core  # noqa: F401

from brain_client.expressive import dsl, liveliness, plan, presets
from brain_client.expressive.basis import Basis
from brain_client.expressive.breathing import Breathing
from brain_client.expressive.prng import Mulberry32

GOLDEN_PATH = Path(__file__).with_name("fixtures") / "golden.json"
CASES = (
    "go 1.5 z=-.8 x=-.6 p=-.7 a=-.3 g=.05 E=.5 | hold 2.5 E=.3 | osc 2 k .15 2 E=.4",
    "go .4 p=.6 a=.4 k=.7 z=.2 g=.3 E=2 | hold 1 E=.5 | go .4 k=-.7 E=2 | hold 1 E=.5 | go .5 k=0",
    "go .5 z=.3 a=.2 E=1 | go .4 z=.8 p=.4 E=1.5 | go .12 z=-.3 p=-.5 a=.6 E=8 | go .4 z=.8 p=.4 E=1.5"
    " | go .12 z=-.3 p=-.5 a=.6 E=8 | go .6 z=0 p=0 a=0 E=1",
    "go .8 b=40 d=.15 E=1 | hold .5 | osc 2 b 15 .8 E=2 | go 1 b=-30 d=-.2 | go .6 b=0 d=0",
    "go .3 g=.9 x=.5 E=3 | osc 1.5 g .3 .4 E=4 | go .2 g=0 x=-.4 E=9 | hold .8 E=6 | go 1 g=.15 x=0 E=.5",
    "go .05 a=1 | hold .05 | go .15 x=1 z=-1 p=1 k=-1 b=-60 d=.25 g=1 E=12 | go .3 a=-1 x=-1 z=1 p=-1 k=1 b=60 d=-.25 g=0 E=0",
    "go .3 p=.5 E=1 | osc 2.4 p .3 .6 | go .4 p=0",
    "osc 3 a .5 1.2 E=2 | osc 2 z .4 .9 | osc 2 d .1 1.5 E=.2 | hold 1 E=0",
    "hold .4",
    "go .3 p=.3 E=1 | osc 2 nod .3 .6 | osc 1.5 sway .3 .9 E=2 | osc 1.2 chatter .3 .4 | go .4 p=0 g=.15",
)
ERRORS = (
    "",
    "go 1 q=1",
    "hold 1 p=.3",
    "osc 1 p .2 .1",
    "go 11 p=1",
    "go .2 p=2",
    "jump 1",
    "osc 2 E 1 1",
    "osc 2 wiggle .2 .8",
    "go x p=1",
    "osc 2 p .3",
    "osc 2 p 3 1",
    "osc 2 p .3 1 z=.5",
    "go 1 p",
    "hold 10 | hold 10 | hold 10 | hold 1",
)
ALIASES = tuple(
    (
        f"go .3 p=.2 | osc 2 {word} {'10' if letter == 'b' else '.2'} .8 E=2",
        f"go .3 p=.2 | osc 2 {letter} {'10' if letter == 'b' else '.2'} .8 E=2",
    )
    for word, letter in dsl.OSC_ALIASES.items()
)
BREATHING_TIMES = (0.0, 1.3, 2.5, 7.7, 12.1, 33.3)
VARIANTS = ("go .4 p=.6 a=.4 k=.7 z=.2 g=.3 E=2 | hold 1 E=.5 | osc 2 z .3 .8 E=3 | go .5 k=0", 3, 7)


def build(basis: Basis | None = None) -> dict[str, object]:
    basis = basis or Basis.load()
    cases = []
    for i, recipe in enumerate(CASES):
        frames = dsl.expand(recipe)
        the_plan = plan.to_plan(frames)
        plan_frames = plan.frames(the_plan)
        motion = liveliness.animate(plan_frames, seed=i)
        actuators = [basis.synthesize(row).as_dict() for row in motion]
        limited = basis.limit_frames(basis.synthesize_frames(motion), 1.0 / 25)
        cases.append(
            {
                "recipe": recipe,
                "frames": frames.tolist(),
                "plan": the_plan,
                "plan_frames": plan_frames.tolist(),
                "seed": i,
                "motion": motion.tolist(),
                "actuators": actuators,
                "limited": limited.tolist(),
            }
        )
    recipe, n, seed = VARIANTS
    rng = Mulberry32(0)
    return {
        "about": "dsl.expand -> plan.to_plan (kdt .25, fc 2) -> plan.frames -> liveliness.animate(seed) -> "
        "basis.synthesize (limits + shoulder clearance, no mujoco projection); limited = basis.limit_frames("
        "actuator rows, 1/25 s), the robot's speed caps; see expressive/golden.py",
        "basis_version": basis.version,
        "fps": 25,
        "mulberry32_seed0": [rng.random() for _ in range(8)],
        "cases": cases,
        "errors": [{"recipe": r, "error": dsl.check(r)} for r in ERRORS],
        "variants": {"recipe": recipe, "n": n, "seed": seed, "plans": dsl.variants(recipe, n, seed)},
        "aliases": [
            {"recipe": alias, "letters": letters, "same": bool((dsl.expand(alias) == dsl.expand(letters)).all())}
            for alias, letters in ALIASES
        ],
        "breathing": [{"t": t, "row": Breathing().sample(t).tolist()} for t in BREATHING_TIMES],
        "presets": [
            {"name": p.name, "prompt": p.prompt, "idea": p.idea, "recipe": p.recipe, "keywords": list(p.keywords)}
            for p in presets.PRESETS.values()
        ],
        "default_preset": presets.DEFAULT,
    }


def write(path: Path = GOLDEN_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build()))
    return path
