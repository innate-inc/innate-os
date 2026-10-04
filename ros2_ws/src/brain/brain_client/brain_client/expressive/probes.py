# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Probe suite: prompts with automatic PHYSICAL checks on the expanded recipe (Binh Pham's 16, moved
onto MARS's channels, plus MARS-specific ones).

Averaged plan metrics miss the failure that matters ("sneezing" snapping the head UP); each probe
asks one concrete question about the motion. A planner's score is the pass rate over several samples
per prompt. ``TEACHER`` holds one reference recipe per probe that passes it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from brain_client.expressive.channels import FPS, Ch, Frames
from brain_client.expressive.dsl import RecipeError, expand
from brain_client.expressive.prng import Mulberry32

A, X, Z, P, K, B, D, G, E = (int(c) for c in Ch)
Check = Callable[[Frames], bool]


def frames_for(recipe: str, seed: int | None = None) -> Frames:
    """Expanded (T, 9) frames; with ``seed`` the timing/amplitude jitter of ``dsl.variants`` applies."""
    return expand(recipe, Mulberry32(seed) if seed is not None else None)


def _half_cycles(x: Frames, amp: float) -> int:
    """Swings of at least ``amp`` around the running mean."""
    k = max(3, len(x) // 10)
    if len(x) <= k:
        return 0
    detrended = x - np.convolve(x, np.ones(k) / k, "same")
    signs = np.sign(np.where(np.abs(detrended) > amp / 2, detrended, 0))
    signs = signs[signs != 0]
    return int((np.diff(signs) != 0).sum()) if len(signs) else 0


def _release_window(f: Frames) -> tuple[Frames, Frames]:
    """(pose just before the energy peak, frames around the peak)."""
    i = int(np.argmax(f[:, E]))
    return f[max(0, i - 10)], f[max(0, i - 2) : i + 8]


def sneeze(f: Frames) -> bool:
    pre, burst = _release_window(f)
    return burst[:, P].min() - pre[P] < -0.25 and burst[:, A].max() - pre[A] > 0.2


def startle(f: Frames) -> bool:
    pre, burst = _release_window(f)
    jolt = burst[:, Z].max() - pre[Z] >= 0.25 or burst[:, A].min() - pre[A] <= -0.25
    alert = burst[:, P].max() - pre[P] >= 0.2 or burst[:, G].max() >= 0.5
    return jolt and alert


def bow(f: Frames) -> bool:
    return f[:, P].min() <= -0.6 and f[:, Z].min() <= -0.3 and f[-1, P] >= -0.3


def sleepy_toddler(f: Frames) -> bool:
    droops = [i for i in range(0, len(f), 5) if f[i, P] <= -0.5]
    recovers = any(f[i:, P].max() >= f[i, P] + 0.35 for i in droops)
    return f[:, Z].min() <= -0.3 and recovers


def stalk(f: Frames) -> bool:
    still = int((f[:, E] <= 0.6).sum()) >= FPS
    return f[:, Z].min() <= -0.3 and still and (f[:, D].max() >= 0.05 or f[:, A].max() >= 0.3)


def yawn(f: Frames) -> bool:
    i = int(np.argmax(f[:, G]))
    return f[i, G] >= 0.7 and f[i, P] >= 0.2 and f[i:, P].min() <= f[i, P] - 0.3


@dataclass(frozen=True)
class Probe:
    prompt: str
    check: Check
    expects: str


PROBES: tuple[Probe, ...] = (
    Probe(
        "sneezing. You build up and then release a sudden sharp sneeze.",
        sneeze,
        "release: gaze DOWN + arm snaps forward",
    ),
    Probe("a big sneeze is coming. Ah... ah... choo!", sneeze, "release: gaze DOWN + arm snaps forward"),
    Probe("startled. A sudden loud noise just made you jump.", startle, "jolt up/back, alert"),
    Probe("bowing deeply. You thank the audience for coming.", bow, "gaze down + low, then back up"),
    Probe(
        "nodding yes. You agree with everything enthusiastically.",
        lambda f: _half_cycles(f[:, P], 0.2) >= 4,
        ">= 2 attend oscillations",
    ),
    Probe(
        "shaking your head no. You refuse firmly.",
        lambda f: _half_cycles(f[:, B], 8.0) >= 4,
        ">= 2 orient oscillations (the base is MARS's head turn)",
    ),
    Probe(
        "looking up at the stars. The night sky is beautiful.",
        lambda f: (f[:, P] >= 0.5).mean() >= 0.4,
        "gaze up >= 40% of the time",
    ),
    Probe(
        "cowering in fear. Something huge looms over you.",
        lambda f: ((f[:, Z] <= -0.3) & (f[:, X] <= -0.3)).mean() >= 0.25,
        "low AND contracted, sustained",
    ),
    Probe(
        "heartbroken. You have just received news that devastated you.",
        lambda f: ((f[:, Z] <= -0.4) & (f[:, P] <= -0.4)).mean() >= 0.25,
        "low AND gaze down, sustained",
    ),
    Probe(
        "ecstatic. You are overjoyed and can barely contain yourself.",
        lambda f: f[:, Z].max() >= 0.5 and (f[:, X].max() >= 0.4 or f[:, G].max() >= 0.5) and f[:, E].max() >= 5,
        "tall, open, high energy",
    ),
    Probe(
        "sleepy toddler. You are fighting to stay awake and keep nodding off.",
        sleepy_toddler,
        "droops AND recovers at least once",
    ),
    Probe(
        "drunk. You are unsteady and your movements are loose and uncoordinated.",
        lambda f: float(np.ptp(f[:, K])) >= 0.6 and len(f) / FPS >= 5,
        "big askew wobble, >= 5 s",
    ),
    Probe(
        "a cat stalking prey. You crouch low, freeze, and creep forward slowly.",
        stalk,
        "low, >= 1 s still, creeps forward",
    ),
    Probe(
        "jumping for joy. You just got the best news ever.",
        lambda f: float(np.ptp(f[:, Z])) >= 0.6 and f[:, E].max() >= 5,
        "big rise change, high energy",
    ),
    Probe("yawning widely. You are so sleepy.", yawn, "mouth wide with gaze up, then droop"),
    Probe(
        "checking both ways. You look left and right before crossing.",
        lambda f: f[:, B].max() >= 15 and f[:, B].min() <= -15,
        "turns both ways",
    ),
    Probe(
        "look at the person. Someone just walked up to you.",
        lambda f: f[:, P].max() >= 0.4 and f[len(f) // 2 :, P].mean() >= 0.2,
        "gaze rises onto the face and stays",
    ),
    Probe(
        "step back in fear. Something startled you.",
        lambda f: f[:, D].min() <= -0.08,
        "the base backs away",
    ),
)

# Concepts in no planner training data: the generalisation score. The rest have close relatives.
OOD_CORE = frozenset(
    {
        "sneezing",
        "a big sneeze is coming",
        "startled",
        "heartbroken",
        "ecstatic",
        "sleepy toddler",
        "drunk",
        "a cat stalking prey",
    }
)

TEACHER: dict[str, str] = {
    PROBES[0].prompt: "go 1 p=.5 a=-.4 z=.4 g=.6 E=1 | hold .4 E=2 | go .15 p=-.7 a=.7 z=-.2 g=.1 E=9"
    " | hold .5 E=2 | go 1 p=0 a=0 z=0 g=.15 E=.8",
    PROBES[1].prompt: "go .6 p=.3 a=-.2 g=.4 E=1 | go .5 p=.5 a=-.35 z=.3 g=.6 E=1.5 | go .5 p=.7 a=-.5 z=.4 g=.8 E=2"
    " | go .15 p=-.8 a=.8 z=-.3 g=.05 E=10 | hold .6 E=2 | go 1 p=0 a=0 z=0 g=.15 E=.8",
    PROBES[2].prompt: "go .5 E=.5 | hold .4 | go .15 z=.7 a=-.6 p=.6 g=.8 x=.4 d=-.08 E=9 | hold 1 E=1.5"
    " | go 1 z=.1 a=0 p=.2 g=.3 x=0 d=-.04 E=1",
    PROBES[3].prompt: "go .4 p=.2 E=1 | go 1 p=-.9 z=-.6 a=.3 E=.6 | hold 1 E=.3 | go .8 p=0 z=0 a=0 E=.8",
    PROBES[4].prompt: "go .3 p=.4 a=.3 E=1.5 | osc 3 p .35 .6 E=1.5 | go .5 p=.2 a=.1 E=.8",
    PROBES[5].prompt: "go .3 p=.2 a=-.2 E=1.5 | osc 3 b 15 .6 E=2 | go .5 b=0 a=0 E=.8",
    PROBES[6].prompt: "go 1.2 p=1 z=.5 a=-.1 g=.3 E=.5 | hold 2 E=.3 | osc 2 k .2 2 E=.4 | hold 1",
    PROBES[
        7
    ].prompt: "go .3 z=-.5 x=-.7 a=-.6 p=.4 d=-.1 g=0 E=7 | hold 1.5 E=5 | go .6 z=-.7 x=-.8 d=-.15 E=6 | hold 1.2 E=5",
    PROBES[8].prompt: "go .3 z=.2 p=.3 E=1 | go 1.5 z=-.8 p=-.9 x=-.6 a=-.2 g=.05 E=.5 | hold 1.5 E=2"
    " | osc 2 z .12 .9 E=3 | hold 1 E=.4",
    PROBES[9].prompt: "go .25 z=.9 x=.8 p=.8 g=.9 E=7 | osc 1.6 z .2 .4 E=8 | osc 1.6 b 15 .5 E=8"
    " | go .5 z=.6 x=.5 g=.6 b=0 E=4 | hold .5 E=3",
    PROBES[10].prompt: "go 1.5 z=-.4 p=-.5 x=-.3 g=.1 E=.4 | go 1 z=-.8 p=-.9 E=.2 | go .25 z=-.1 p=.1 E=3"
    " | hold .6 E=1 | go 1.5 z=-.8 p=-.9 E=.2 | go .25 z=-.1 p=0 E=3 | hold .5 | go 1.8 z=-.85 p=-1 E=.1",
    PROBES[11].prompt: "go .8 k=.6 z=-.2 p=-.2 E=2 | osc 3 k .5 1.6 E=2.5 | go .8 k=-.5 b=-12 d=.05 E=2"
    " | go .8 k=.4 b=10 d=0 E=2 | osc 2 z .2 1.4 E=2 | go 1 k=0 b=0 z=0 p=0 E=1",
    PROBES[12].prompt: "go .8 z=-.6 p=.4 a=.4 x=-.3 g=.1 E=.4 | hold 1.2 E=.1 | go 2 d=.12 a=.6 E=.3 | hold .8 E=.05"
    " | go .15 a=1 d=.2 g=.9 z=-.2 E=8 | go .8 a=.3 g=.2 z=0 E=1",
    PROBES[13].prompt: "go .3 z=-.4 a=-.2 E=2 | go .2 z=.9 x=.7 p=.7 g=.8 E=8 | osc 1.6 z .3 .4 E=8 | go .3 z=-.2 E=4"
    " | go .2 z=.9 E=8 | go .6 z=.4 x=.3 E=3",
    PROBES[14].prompt: "go 1 p=.5 z=.4 g=.9 a=-.2 E=.5 | hold .8 E=.3 | go 1.2 p=-.5 z=-.4 g=.1 a=0 E=.3 | hold 1 E=.2",
    PROBES[15].prompt: "go .5 p=.3 E=1 | go .7 b=-35 | hold .6 | go 1 b=35 | hold .6 | go .7 b=0 | hold .4",
    PROBES[16].prompt: "go .6 p=.7 a=.3 z=.2 g=.25 E=1 | hold 1.5 E=.6 | go .4 k=.3 E=1 | hold 1 | go .6 p=.5 k=0 E=.6",
    PROBES[17].prompt: "go .15 a=-.7 d=-.15 x=-.5 z=-.2 p=.3 g=0 E=8 | hold .8 E=5 | go .6 d=-.22 E=4 | hold 1 E=4"
    " | go 1 a=-.3 x=-.3 E=2",
}


def passes(probe: Probe, recipe: str | None, seed: int | None = None) -> bool:
    if not recipe:
        return False
    try:
        return bool(probe.check(frames_for(recipe, seed)))
    except RecipeError:
        return False


def score(recipes_per_prompt: Mapping[str, Sequence[str | None]]) -> tuple[float, dict[str, float]]:
    """``{prompt: [recipe or None, ...]}`` -> (mean pass rate over probes, pass rate per prompt)."""
    per: dict[str, float] = {}
    for probe in PROBES:
        results = [passes(probe, recipe) for recipe in recipes_per_prompt.get(probe.prompt, [])]
        per[probe.prompt] = float(np.mean(results)) if results else 0.0
    return float(np.mean(list(per.values()))), per


def split(per: Mapping[str, float]) -> tuple[float, float]:
    """(out-of-distribution score, skill score) of a ``score`` breakdown."""
    core = [v for p, v in per.items() if p.split(".")[0] in OOD_CORE]
    skill = [v for p, v in per.items() if p.split(".")[0] not in OOD_CORE]
    return float(np.mean(core)), float(np.mean(skill))
