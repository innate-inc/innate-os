# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The procedural generator: plan -> lively 25 Hz motion, deterministic for a seed (the CPU stand-in
for a learned generator, and its fallback).

Three layers on top of the linearly interpolated plan:

1. Timing jitter: the plan is read through a smooth time warp ``w(t) = t + J sin(pi t / D) * mean_k
   sin(2 pi g_k t + psi_k)``, so beats never land on a metronome grid (``J`` = 50 ms, 2 terms).
2. A second-order tracker per channel, critically damped on the tracking error with velocity
   feed-forward: it follows slow segments with no lag and carries momentum past the end of a fast
   one, overshooting by roughly ``speed / (e * omega)``. Integrated with ``SUBSTEPS`` semi-implicit
   Euler steps per frame on the linearly interpolated target.
3. Band-limited noise per channel scaled by energy: ``E(t) * NOISE_SCALE[c] * sqrt(2/K) * sum_k
   sin(phase_k(t))``, where each sinusoid's frequency is log-uniform in the channel's band and its
   phase advances by ``2 pi f_k (1 + E / 6) / FPS`` per frame (livelier = faster as well as bigger).

Random draws come from ``Mulberry32(seed)`` in this order: for the 2 warp terms, frequency then
phase; then for each of the 8 channels in order and each of its ``K`` sinusoids, frequency then phase.
A uniform ``u`` becomes a frequency ``lo * (hi / lo) ** u`` and a phase ``2 pi u``.
"""

from __future__ import annotations

import math

import numpy as np

from brain_client.expressive.channels import FPS, MOTION_CHANNELS, Ch, Frames, clip_to_limits
from brain_client.expressive.plan import ENERGY_SCALE
from brain_client.expressive.prng import Mulberry32

SUBSTEPS = 8
NOISE_TERMS = 5
WARP_S = 0.05
WARP_BAND_HZ = (0.15, 0.4)
NOISE_SCALE = ENERGY_SCALE
# approach expand rise attend askew orient advance grip
OMEGA: Frames = np.array([14.0, 18.0, 14.0, 22.0, 16.0, 9.0, 7.0, 30.0])
NOISE_BAND_HZ: tuple[tuple[float, float], ...] = (
    (0.5, 2.0),
    (0.5, 2.0),
    (0.4, 2.0),
    (0.7, 3.0),
    (0.5, 2.0),
    (0.2, 0.8),
    (0.2, 0.8),
    (1.0, 4.0),
)


def _draw_frequency(rng: Mulberry32, band: tuple[float, float]) -> float:
    lo, hi = band
    return lo * (hi / lo) ** rng.random()


def _warp(count: int, rng: Mulberry32) -> Frames:
    """Frame-index positions to read the plan at: monotonic, pinned at both ends."""
    t = np.arange(count) / FPS
    duration = max(t[-1], 1.0 / FPS)
    wobble = np.zeros(count)
    for _ in range(2):
        frequency = _draw_frequency(rng, WARP_BAND_HZ)
        phase = 2 * math.pi * rng.random()
        wobble += np.sin(2 * math.pi * frequency * t + phase) / 2
    warped = t + WARP_S * np.sin(np.pi * t / duration) * wobble
    return np.clip(warped * FPS, 0, count - 1)


def _track(target: Frames) -> Frames:
    """Critically damped error dynamics with target-velocity feed-forward, per channel."""
    h = 1.0 / FPS / SUBSTEPS
    x = target[0].copy()
    v = np.zeros_like(x)
    out = np.empty_like(target)
    out[0] = x
    for i in range(1, len(target)):
        start, step = target[i - 1], target[i] - target[i - 1]
        target_velocity = step * FPS
        for s in range(1, SUBSTEPS + 1):
            r = start + step * (s / SUBSTEPS)
            acceleration = OMEGA * OMEGA * (r - x) + 2 * OMEGA * (target_velocity - v)
            v = v + acceleration * h
            x = x + v * h
        out[i] = x
    return out


def _noise(energy: Frames, rng: Mulberry32) -> Frames:
    tempo_phase = np.concatenate([[0.0], np.cumsum(1.0 + energy[:-1] / 6.0)]) / FPS
    out = np.zeros((len(energy), MOTION_CHANNELS))
    for c in range(MOTION_CHANNELS):
        for _ in range(NOISE_TERMS):
            frequency = _draw_frequency(rng, NOISE_BAND_HZ[c])
            phase = 2 * math.pi * rng.random()
            out[:, c] += np.sin(2 * math.pi * frequency * tempo_phase + phase)
        out[:, c] *= math.sqrt(2.0 / NOISE_TERMS) * NOISE_SCALE[c] * energy
    return out


def animate(plan_frames: Frames, seed: int = 0) -> Frames:
    """(T, 9) interpolated plan (``plan.frames``) -> (T, 8) motion at FPS."""
    rng = Mulberry32(seed)
    positions = _warp(len(plan_frames), rng)
    grid = np.arange(len(plan_frames))
    target = np.stack([np.interp(positions, grid, plan_frames[:, c]) for c in range(MOTION_CHANNELS)], -1)
    energy = np.maximum(plan_frames[:, Ch.ENERGY], 0.0)
    return clip_to_limits(_track(target) + _noise(energy, rng))
