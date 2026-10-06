# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Playback-timed loudness envelopes and additive speech motion in plan space."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

from brain_client.expressive.channels import MOTION_CHANNELS, Ch
from brain_client.expressive.prng import Mulberry32

if TYPE_CHECKING:
    from numpy.typing import NDArray

    from brain_client.expressive.basis import Vector
    from brain_client.expressive.channels import Frames

HOP_S = 0.01
DB_FLOOR = -50.0
DB_CEIL = -22.0
LOUDNESS_GAMMA = 0.9
SWAY_ATTACK_S = 0.05
SWAY_RELEASE_S = 0.25
ACCENT_ATTACK_S = 0.015
ACCENT_RELEASE_S = 0.08
ACCENT_ATTEND = 0.15
HISTORY_S = 30.0
_RELEASES = np.array([SWAY_RELEASE_S, ACCENT_RELEASE_S])
# (channel, Hz, amplitude at full loudness). Rise and approach are kept small: from the folded
# NEUTRAL their endpoints swing j3 by ~3 rad, so 0.02 is already a visible bob.
SWAY: tuple[tuple[Ch, float, float], ...] = (
    (Ch.RISE, 0.25, 0.02),
    (Ch.ASKEW, 1.3, 0.08),
    (Ch.ATTEND, 2.2, 0.06),
    (Ch.APPROACH, 0.35, 0.012),
)


def _to_mono(pcm: NDArray[np.generic]) -> Vector:
    if pcm.ndim not in (1, 2):
        raise ValueError("PCM must be mono or a two-dimensional channel array")
    if pcm.size == 0:
        return np.empty(0, dtype=np.float64)
    audio = np.asarray(pcm, dtype=np.float64)
    if np.issubdtype(pcm.dtype, np.integer):
        audio /= float(np.iinfo(pcm.dtype.name).max)
    if audio.ndim == 2:
        channel_axis = 1 if audio.shape[1] <= max(2, audio.shape[0]) else 0
        audio = audio.mean(axis=channel_axis)
    if not np.all(np.isfinite(audio)):
        raise ValueError("PCM samples must be finite")
    return audio


class SpeechSway:
    def __init__(self, latency_s: float = 0.0, seed: int = 7) -> None:
        self.latency_s = latency_s
        rng = Mulberry32(seed)
        self._phases = tuple(rng.uniform(0.0, math.tau) for _ in SWAY)
        self._envelopes: Frames = np.empty((0, 3), dtype=np.float64)
        self._state: Vector = np.zeros(2, dtype=np.float64)
        self._carry: Vector = np.empty(0, dtype=np.float64)
        self._sample_rate = 0
        self._end = -math.inf
        self._playback: list[tuple[float, float]] = []

    @property
    def playing_until(self) -> float:
        """End of the queued audio; latency shifts motion sampling, not playback."""
        return self._end

    def speaking(self, t: float) -> bool:
        return any(start <= t < end for start, end in self._playback)

    def feed(self, pcm: NDArray[np.generic], sample_rate: int, at: float) -> None:
        """Append PCM at playback speed, retaining partial analysis hops across chunks."""
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        audio = _to_mono(pcm)
        if audio.size == 0:
            return
        start = max(at, self._end)
        if start > self._end:
            self._gap(start)
        if sample_rate != self._sample_rate:
            self._carry = np.empty(0, dtype=np.float64)
        self._sample_rate = sample_rate
        hop = max(1, round(HOP_S * sample_rate))
        stream_start = start - len(self._carry) / sample_rate
        samples = np.concatenate((self._carry, audio))
        count = len(samples) // hop
        self._carry = samples[count * hop :].copy()
        self._end = start + len(audio) / sample_rate
        if self._playback and start <= self._playback[-1][1]:
            self._playback[-1] = (self._playback[-1][0], self._end)
        else:
            self._playback.append((start, self._end))
        if count:
            frames = samples[: count * hop].reshape(count, hop)
            db = 20.0 * np.log10(np.sqrt(np.mean(frames * frames, axis=1)) + 1e-9)
            levels = np.clip((db - DB_FLOOR) / (DB_CEIL - DB_FLOOR), 0.0, 1.0) ** LOUDNESS_GAMMA
            self._analyze(levels, stream_start, hop / sample_rate)
        self._prune(at)

    def interrupt(self, at: float) -> None:
        """Discard unplayed PCM while allowing the current envelopes to release."""
        if at >= self._end:
            return
        self._state = self._envelopes_at(at)
        self._envelopes = self._envelopes[self._envelopes[:, 0] < at]
        self._append(np.array([[at, *self._state]]))
        self._playback = [(start, min(end, at)) for start, end in self._playback if start < at]
        self._end = at
        self._carry = np.empty(0, dtype=np.float64)

    def loudness(self, t: float) -> float:
        return float(self._envelopes_at(t - self.latency_s)[0])

    def sample(self, t: float) -> Vector:
        row = np.zeros(MOTION_CHANNELS, dtype=np.float64)
        audio_t = t - self.latency_s
        sway, fast = self._envelopes_at(audio_t)
        for (channel, frequency, amplitude), phase in zip(SWAY, self._phases, strict=True):
            row[channel] = sway * amplitude * math.sin(math.tau * frequency * audio_t + phase)
        row[Ch.ATTEND] -= ACCENT_ATTEND * max(0.0, fast - sway)
        return row

    def _gap(self, start: float) -> None:
        last = float(self._envelopes[-1, 0]) if len(self._envelopes) else start
        times = last + np.linspace(0.0, 5.0 * SWAY_RELEASE_S, 21)[1:]
        times = np.append(times[times < start], start)
        decay = self._state * np.exp(-(times[:, None] - last) / _RELEASES)
        self._append(np.column_stack((times, decay)))
        self._state = decay[-1].copy()
        self._carry = np.empty(0, dtype=np.float64)

    def _analyze(self, levels: Vector, start: float, dt: float) -> None:
        attacks = 1.0 - np.exp(-dt / np.array([SWAY_ATTACK_S, ACCENT_ATTACK_S]))
        releases = 1.0 - np.exp(-dt / _RELEASES)
        rows = np.empty((len(levels), 3), dtype=np.float64)
        rows[:, 0] = start + (np.arange(len(levels)) + 1) * dt
        for i, level in enumerate(levels):
            self._state += (level - self._state) * np.where(level > self._state, attacks, releases)
            rows[i, 1:] = self._state
        self._append(rows)

    def _envelopes_at(self, t: float) -> Vector:
        rows = self._envelopes
        if not len(rows) or t < rows[0, 0]:
            return np.zeros(2, dtype=np.float64)
        if t >= rows[-1, 0]:
            return rows[-1, 1:] * np.exp(-(t - rows[-1, 0]) / _RELEASES)
        return np.array([np.interp(t, rows[:, 0], rows[:, column]) for column in (1, 2)])

    def _append(self, rows: Frames) -> None:
        self._envelopes = np.concatenate((self._envelopes, rows))

    def _prune(self, at: float) -> None:
        cutoff = at - max(0.0, self.latency_s) - HISTORY_S
        index = max(0, int(np.searchsorted(self._envelopes[:, 0], cutoff)) - 1)
        self._envelopes = self._envelopes[index:]
        self._playback = [(start, end) for start, end in self._playback if end >= cutoff]
