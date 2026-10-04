# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Motion recipes: the tiny keyframe language the planner writes, expanded into per-frame channels.

A recipe is segments separated by ``|``, starting from NEUTRAL::

    go D k=v ...              cosine-ease to the targets over D s (0.15-0.3 s = a snap)
    hold D [E=v]              stay in the pose (optionally easing the energy)
    osc D ch amp per [E=v]    sinusoid on channel ch (amplitude amp, period per >= 0.3 s) around the pose;
                              ch is a letter or its gesture word (nod bob sway lean turn chatter)

Keys are the DSL letters of ``channels.CHANNELS`` (a x z p k b d g E). Each segment lasts 0.05-10 s
and the written total at most 30 s.

Randomised expansion (``variants``) draws from one ``Mulberry32`` stream in this order: per variant,
amplitude scale then tempo scale; then per segment in recipe order a duration jitter, and for ``osc``
an amplitude jitter followed by a period jitter. ``expand`` without a stream is exact.
"""

from __future__ import annotations

import math

import numpy as np

from brain_client.expressive.channels import CHANNELS, DSL_INDEX, FPS, HIGH, LOW, NEUTRAL, Ch, Frames, clip_to_limits
from brain_client.expressive.plan import SERVE_FC, SERVE_KDT, Plan, frame_count, to_plan
from brain_client.expressive.prng import Mulberry32

MIN_SEGMENT_S = 0.05
MAX_SEGMENT_S = 10.0
MAX_TOTAL_S = 30.0
MIN_OSC_PERIOD_S = 0.3
OSC_RAMP_S = 0.3
# The prompt names each osc channel by its gesture ("nod (p)"); planners often write the word.
OSC_ALIASES = {"nod": "p", "bob": "z", "sway": "k", "lean": "a", "turn": "b", "chatter": "g"}
_SCALED = {Ch.APPROACH, Ch.EXPAND, Ch.RISE, Ch.ATTEND, Ch.ASKEW, Ch.ORIENT, Ch.ADVANCE}
_KEYS = " ".join(c.dsl for c in CHANNELS)


class RecipeError(ValueError):
    pass


def _number(token: str, segment: str) -> float:
    try:
        value = float(token)
    except ValueError:
        raise RecipeError(f"bad number {token!r} in {segment!r}") from None
    if not math.isfinite(value):
        raise RecipeError(f"bad number {token!r} in {segment!r}")
    return value


def _targets(tokens: list[str], target: Frames, amp: float, segment: str) -> None:
    for token in tokens:
        if "=" not in token:
            raise RecipeError(f"expected key=value, got {token!r} in {segment!r}")
        key, raw = token.split("=", 1)
        if key not in DSL_INDEX:
            raise RecipeError(f"unknown channel {key!r} (use {_KEYS})")
        index = DSL_INDEX[key]
        value = _number(raw, segment)
        if not LOW[index] <= value <= HIGH[index]:
            raise RecipeError(f"{key}={value:g} outside [{LOW[index]:g}, {HIGH[index]:g}]")
        target[index] = value * amp if index in _SCALED else value


def _cosine_ease(start: Frames, target: Frames, count: int) -> Frames:
    w = 0.5 - 0.5 * np.cos(np.pi * np.arange(1, count + 1) / count)
    return start + w[:, None] * (target - start)


def _osc(tokens: list[str], current: Frames, count: int, amp: float, rng: Mulberry32 | None, segment: str) -> Frames:
    if len(tokens) < 5:
        raise RecipeError(f"osc needs: osc D ch amp period, got {segment!r}")
    channel = OSC_ALIASES.get(tokens[2], tokens[2])
    if channel not in DSL_INDEX or channel == "E":
        raise RecipeError(f"osc on unknown channel {channel!r} (use {_KEYS[:-2]} or {' '.join(OSC_ALIASES)})")
    index = DSL_INDEX[channel]
    amplitude, period = _number(tokens[3], segment), _number(tokens[4], segment)
    if period < MIN_OSC_PERIOD_S:
        raise RecipeError("osc period below 0.3 s: fast shaking belongs in E (energy), not osc")
    if abs(amplitude) > HIGH[index] - LOW[index]:
        raise RecipeError(f"osc amplitude {amplitude:g} too big for {channel} (range {LOW[index]:g}..{HIGH[index]:g})")
    target = current.copy()
    _targets(tokens[5:], target, amp, segment)
    if np.any(target[: Ch.ENERGY] != current[: Ch.ENERGY]):
        raise RecipeError(f"osc only changes energy (E=v); move with go: {segment!r}")
    if index in _SCALED:
        amplitude *= amp
    if rng is not None:
        amplitude *= rng.uniform(0.8, 1.2)
        period *= rng.uniform(0.85, 1.15)
    u = np.arange(1, count + 1) / FPS
    envelope = np.minimum(1.0, np.minimum(u, u[-1] - u + 1.0 / FPS) / OSC_RAMP_S)
    rows = np.repeat(current[None], count, 0)
    rows[:, Ch.ENERGY] = np.linspace(current[Ch.ENERGY], target[Ch.ENERGY], count)
    rows[:, index] += amplitude * np.sin(2 * np.pi * u / period) * envelope
    return rows


def expand(recipe: str, rng: Mulberry32 | None = None, amp: float = 1.0, tempo: float = 1.0) -> Frames:
    """Recipe -> (T, 9) per-frame channels at FPS, first row NEUTRAL, clamped to the channel ranges.

    ``amp`` scales the posture targets (not grip or energy), ``tempo`` the durations (>1 = slower);
    with ``rng`` each segment's timing gets +-15 % jitter and each osc +-20 % amplitude.
    """
    segments = [s.strip() for s in recipe.split("|") if s.strip()]
    if not segments:
        raise RecipeError("empty recipe")
    current = NEUTRAL.copy()
    rows: list[Frames] = [current[None]]
    written = 0.0
    for segment in segments:
        tokens = segment.split()
        command = tokens[0]
        if command not in ("go", "hold", "osc") or len(tokens) < 2:
            raise RecipeError(f"bad segment {segment!r} (use go, hold, osc)")
        seconds = _number(tokens[1], segment)
        if not MIN_SEGMENT_S <= seconds <= MAX_SEGMENT_S:
            raise RecipeError(f"duration {seconds:g} outside [{MIN_SEGMENT_S}, {MAX_SEGMENT_S:g}] s")
        written += seconds
        if written > MAX_TOTAL_S + 1e-9:
            raise RecipeError(f"recipe lasts {written:.1f} s; keep it under {MAX_TOTAL_S:g} s")
        seconds *= tempo * (rng.uniform(0.85, 1.15) if rng is not None else 1.0)
        count = max(1, frame_count(seconds))
        if command == "osc":
            block = _osc(tokens, current, count, amp, rng, segment)
        else:
            target = current.copy()
            _targets(tokens[2:], target, amp, segment)
            if command == "hold" and np.any(target[: Ch.ENERGY] != current[: Ch.ENERGY]):
                raise RecipeError(f"hold only changes energy (E=v); move with go: {segment!r}")
            block = _cosine_ease(current, target, count)
        rows.append(block)
        current = block[-1].copy()
    return clip_to_limits(np.concatenate(rows))


def check(recipe: str) -> str | None:
    """None if the recipe is valid, else the error message (the planner's repair hint)."""
    try:
        expand(recipe)
    except RecipeError as e:
        return str(e)
    return None


def variants(recipe: str, n: int, seed: int = 0, kdt: float = SERVE_KDT, fc: float | None = SERVE_FC) -> list[Plan]:
    """``n`` randomised plans of one recipe: amplitude x0.75-1.25, tempo x0.8-1.25, per-segment jitter."""
    rng = Mulberry32(seed)
    plans: list[Plan] = []
    for _ in range(n):
        amp = rng.uniform(0.75, 1.25)
        tempo = rng.uniform(0.8, 1.25)
        plans.append(to_plan(expand(recipe, rng, amp=amp, tempo=tempo), kdt=kdt, fc=fc))
    return plans
