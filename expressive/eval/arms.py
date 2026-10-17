"""The arms under comparison: one recipe, different ways to turn its plan into motion.

An arm maps conditioning frames (T, 9 at 25 Hz) and a seed to plan-space motion (T, 8) at 25 Hz. The
built-in arms are conditioned on the expanded recipe itself (``dsl.expand``, as ``Clip.from_recipe`` ships):
``lively`` (procedural liveliness) and ``direct`` (the bare recipe). Learned generators get the interpolated
serving plan (``plan.frames(plan.to_plan(dsl.expand(recipe)))``), what they are trained on. The ml workstream's flow generator joins with ``--flow CKPT`` (``flow`` below);
any other function with that signature with ``--arm name=module:function``. Arm names must not contain
``-`` or ``.`` (they name the A/B files).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path

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
PROCEDURAL = frozenset(ARMS.values())


def flow(checkpoint: str | Path, device: str | None = None) -> Generator:
    """The ml workstream's flow-matching generator (``ml/generator/sample.py``) as an arm. Needs torch
    (``uv run --extra flow``); plans longer than its window fall back to liveliness inside ``generate_frames``."""
    from ml.generator.sample import Generator as FlowModel

    model = FlowModel(checkpoint, device)

    def generate(plan_frames: Frames, seed: int) -> Frames:
        return model.generate_frames([plan_frames], [seed])[0]

    return generate


def load_arm(spec: str) -> tuple[str, Generator]:
    """``name=module:function`` -> (name, generator)."""
    name, _, target = spec.partition("=")
    module, _, attribute = target.partition(":")
    if not (name and module and attribute) or "-" in name or "." in name:
        raise ValueError(f"--arm wants name=module:function (no '-' or '.' in the name), got {spec!r}")
    return name, getattr(importlib.import_module(module), attribute)


def make_clip(
    recipe: str, generator: Generator, seed: int = 0, name: str = "clip", prompt: str = "", idea: str = ""
) -> Clip:
    """``recipe`` played through ``generator`` (``lively`` matches ``Clip.from_recipe``)."""
    expanded = expand(recipe)
    motion = generator(expanded if generator in PROCEDURAL else frames(to_plan(expanded)), seed)
    return Clip.from_plan_frames(motion, name=name, prompt=prompt, idea=idea, recipe=recipe)
