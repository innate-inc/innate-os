# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""mulberry32: a 32-bit PRNG small enough to reproduce bit-exactly in JavaScript (the studio's port).

Every random draw in recipe expansion and the liveliness layer comes from this stream, never from
numpy, so a seed gives the same motion in Python and in the browser.
"""

from __future__ import annotations

import math

_MASK = 0xFFFFFFFF


class Mulberry32:
    def __init__(self, seed: int) -> None:
        self._state = seed & _MASK

    def random(self) -> float:
        """Uniform in [0, 1); identical to the canonical JS ``mulberry32``."""
        self._state = (self._state + 0x6D2B79F5) & _MASK
        t = self._state
        t = ((t ^ (t >> 15)) * (t | 1)) & _MASK
        t = (((t + (((t ^ (t >> 7)) * (t | 61)) & _MASK)) & _MASK) ^ t) & _MASK
        return ((t ^ (t >> 14)) & _MASK) / 4294967296.0

    def uniform(self, lo: float, hi: float) -> float:
        return lo + (hi - lo) * self.random()

    def gauss(self) -> float:
        """Standard normal by Box-Muller; consumes exactly two draws (u1 then u2), never caches."""
        u1 = self.random()
        u2 = self.random()
        return math.sqrt(-2.0 * math.log(1.0 - u1)) * math.cos(2.0 * math.pi * u2)
