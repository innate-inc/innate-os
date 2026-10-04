# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Motion plan: sparse keyframes of all nine channels, the interface between planner and generator.

A plan says where the body is (posture channels, low-passed) and how lively it is (``energy``), not
the individual wiggles. Served plans key every 0.25 s with posture low-passed at 2 Hz so a snap
stays a snap; plans extracted from recorded motion for generator training key every 0.5 s at 1 Hz.

The low-pass is scipy's ``filtfilt(*butter(2, fc / (fps / 2)), padtype="odd")`` reimplemented in
numpy (the robot has no scipy) and bit-for-bit portable to the studio's JS port.
"""

from __future__ import annotations

import math
from typing import TypedDict

import numpy as np

from brain_client.expressive.channels import CHANNELS, FPS, MOTION_CHANNELS, NEUTRAL, PLAN_KEYS, Ch, Frames

SERVE_KDT = 0.25
SERVE_FC = 2.0
EXTRACT_KDT = 0.5
EXTRACT_FC = 1.0
# Energy is measured as the RMS of the fast (above-fc) detail divided by the liveliness layer's
# per-unit-energy amplitude, so extract(liveliness(plan)) gives back roughly the plan's energy. The arm
# channels are small because the basis turns one unit of rise/approach/expand into ~3 rad of elbow;
# orient and advance get none: the base moves only when the recipe says so.
ENERGY_SCALE: Frames = np.array([0.008, 0.008, 0.008, 0.025, 0.015, 0.0, 0.0, 0.02])


class Plan(TypedDict):
    duration: float
    keys: list[dict[str, float]]


def frame_count(seconds: float, fps: float = FPS) -> int:
    """Frames covering ``seconds``, rounding half up (JS ``Math.floor(x + 0.5)``, not banker's rounding)."""
    return math.floor(seconds * fps + 0.5)


def butter2(fc: float, fps: float = FPS) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """2nd-order Butterworth low-pass ``(b, a)`` by the prewarped bilinear transform, as scipy's ``butter``."""
    k = math.tan(math.pi * fc / fps)
    norm = 1.0 / (1.0 + math.sqrt(2.0) * k + k * k)
    b0 = k * k * norm
    return (b0, 2.0 * b0, b0), (1.0, 2.0 * (k * k - 1.0) * norm, (1.0 - math.sqrt(2.0) * k + k * k) * norm)


def _lfilter(b: tuple[float, float, float], a: tuple[float, float, float], x: Frames, z0: Frames, z1: Frames) -> Frames:
    """Transposed direct form II along axis 0, all columns at once."""
    y = np.empty_like(x)
    for i in range(len(x)):
        yi = b[0] * x[i] + z0
        z0 = b[1] * x[i] - a[1] * yi + z1
        z1 = b[2] * x[i] - a[2] * yi
        y[i] = yi
    return y


def lowpass(x: Frames, fc: float, fps: float = FPS) -> Frames:
    """Zero-phase low-pass of (T, C) columns: scipy ``filtfilt`` with ``padtype="odd"``, ``padlen=min(T-1, 9)``."""
    if len(x) < 16:
        return x.copy()
    b, a = butter2(fc, fps)
    # scipy.signal.lfilter_zi for a 2nd-order filter, solved in closed form.
    r0, r1 = b[1] - a[1] * b[0], b[2] - a[2] * b[0]
    zi0 = (r0 + r1) / (1.0 + a[1] + a[2])
    zi1 = r1 - a[2] * zi0
    pad = min(len(x) - 1, 9)
    ext = np.concatenate([2 * x[0] - x[pad:0:-1], x, 2 * x[-1] - x[-2 : -pad - 2 : -1]])
    y = _lfilter(b, a, ext, zi0 * ext[0], zi1 * ext[0])
    y = _lfilter(b, a, y[::-1], zi0 * y[-1], zi1 * y[-1])[::-1]
    return y[pad:-pad]


def to_plan(frames: Frames, kdt: float = SERVE_KDT, fc: float | None = SERVE_FC) -> Plan:
    """(T, 9) expanded recipe frames -> plan: posture low-passed at ``fc`` Hz, keys every ``kdt`` s.

    Values keep full precision; round only when pretty-printing.
    """
    posture = lowpass(frames[:, :MOTION_CHANNELS], fc) if fc else frames[:, :MOTION_CHANNELS]
    count = len(frames)
    keys: list[dict[str, float]] = []
    for t in key_times(count, kdt):
        i = min(count - 1, frame_count(t))
        key = {"t": t, **{c.key: float(posture[i, j]) for j, c in enumerate(CHANNELS[:MOTION_CHANNELS])}}
        key["energy"] = max(0.0, float(frames[i, Ch.ENERGY]))
        keys.append(key)
    return Plan(duration=(count - 1) / FPS, keys=keys)


def key_times(count: int, kdt: float) -> list[float]:
    """Key times every ``kdt`` over ``count`` frames, plus the last frame when it falls between keys
    (else a recipe's final partial interval, often its release, would be cut off)."""
    duration = (count - 1) / FPS
    times = [k * kdt for k in range(math.floor(duration / kdt + 1e-9) + 1)]
    if duration - times[-1] > 1e-9:
        times.append(duration)
    return times


def frames(plan: Plan, count: int | None = None) -> Frames:
    """Plan -> (T, 9) per-frame conditioning, linearly interpolated between keys (missing channels hold)."""
    count = count or max(2, frame_count(plan["duration"]) + 1)
    last = dict(zip(PLAN_KEYS, NEUTRAL.tolist(), strict=True))
    rows: list[list[float]] = []
    for key in sorted(plan["keys"], key=lambda k: k["t"]):
        last.update({c: float(key[c]) for c in PLAN_KEYS if c in key})
        rows.append([float(key["t"]), *(last[c] for c in PLAN_KEYS)])
    table = np.array(rows)
    times = np.arange(count) / FPS
    return np.stack([np.interp(times, table[:, 0], table[:, 1 + j]) for j in range(len(PLAN_KEYS))], -1)


def extract(motion: Frames, kdt: float = EXTRACT_KDT, fc: float = EXTRACT_FC) -> Plan:
    """(T, 8) motion -> the plan that describes it (posture below ``fc`` + energy of the detail above)."""
    slow = lowpass(motion, fc)
    fast = (motion - slow)[:, ENERGY_SCALE > 0] / ENERGY_SCALE[ENERGY_SCALE > 0]
    count = len(motion)
    window = max(EXTRACT_KDT, kdt)
    keys: list[dict[str, float]] = []
    for t in key_times(count, kdt):
        i = min(count - 1, frame_count(t))
        lo, hi = max(0, math.floor((t - window / 2) * FPS)), min(count, math.floor((t + window / 2) * FPS) + 1)
        energy = float(np.sqrt((fast[lo:hi] ** 2).mean())) if hi > lo else 0.0
        keys.append({"t": t, **{c.key: float(slow[i, j]) for j, c in enumerate(CHANNELS[:MOTION_CHANNELS])}})
        keys[-1]["energy"] = energy
    return Plan(duration=(count - 1) / FPS, keys=keys)
