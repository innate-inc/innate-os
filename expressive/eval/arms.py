"""The arms under comparison: one recipe, different ways to turn its plan into motion.

A generator maps the interpolated plan (T, 9; ``plan.frames``) and a seed to plan-space motion (T, 8) at
25 Hz. The procedural liveliness layer and the bare plan are built in; the ml workstream's flow generator
joins as ``--arm flow=module:function`` with the same signature.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable

import _core  # noqa: F401

from brain_client.expressive.channels import MOTION_CHANNELS, Frames
from brain_client.expressive.dsl import expand
from brain_client.expressive.liveliness import animate
from brain_client.expressive.motion import Clip
from brain_client.expressive.plan import frames, to_plan

Generator = Callable[[Frames, int], Frames]


def direct(plan_frames: Frames, seed: int) -> Frames:  # noqa: ARG001 — the control arm has no randomness
    return plan_frames[:, :MOTION_CHANNELS].copy()


ARMS: dict[str, Generator] = {"lively": animate, "direct": direct}


def load_arm(spec: str) -> tuple[str, Generator]:
    """``name=module:function`` -> (name, generator)."""
    name, _, target = spec.partition("=")
    module, _, attribute = target.partition(":")
    if not (name and module and attribute):
        raise ValueError(f"--arm wants name=module:function, got {spec!r}")
    return name, getattr(importlib.import_module(module), attribute)


def make_clip(
    recipe: str, generator: Generator, seed: int = 0, name: str = "clip", prompt: str = "", idea: str = ""
) -> Clip:
    """The served plan of ``recipe`` played through ``generator`` (``lively`` matches ``Clip.from_recipe``)."""
    motion = generator(frames(to_plan(expand(recipe))), seed)
    return Clip.from_plan_frames(motion, name=name, prompt=prompt, idea=idea, recipe=recipe)
