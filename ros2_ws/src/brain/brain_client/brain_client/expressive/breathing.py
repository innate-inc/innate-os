# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Continuous idle breathing in the eight expressive plan channels."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from brain_client.expressive.channels import MOTION_CHANNELS, NEUTRAL, Ch

if TYPE_CHECKING:
    from brain_client.expressive.basis import Vector


@dataclass(frozen=True)
class Breathing:
    """Small on purpose: from the folded NEUTRAL, more rise/approach (or a lower gaze) brings the arm into
    the head camera's view; the gaze wanders around a slight lift for the same reason."""

    rise_amplitude: float = 0.02
    rise_frequency_hz: float = 0.1
    approach_amplitude: float = 0.02
    approach_frequency_hz: float = 0.1
    attend_center: float = 0.05
    attend_amplitudes: tuple[float, float] = (0.03, 0.02)
    attend_frequencies_hz: tuple[float, float] = (0.07, math.sqrt(3.0) / 10.0)
    grip_amplitude: float = 0.02
    grip_frequency_hz: float = 0.1
    name: str = field(default="breathing", init=False)
    duration: float = field(default=math.inf, init=False)

    def sample(self, t: float) -> Vector:
        row = NEUTRAL[:MOTION_CHANNELS].copy()
        row[Ch.RISE] += self.rise_amplitude * math.sin(math.tau * self.rise_frequency_hz * t)
        row[Ch.APPROACH] += self.approach_amplitude * math.sin(
            math.tau * self.approach_frequency_hz * t - math.pi / 2.0
        )
        row[Ch.ATTEND] += self.attend_center + sum(
            amplitude * math.sin(math.tau * frequency * t)
            for amplitude, frequency in zip(self.attend_amplitudes, self.attend_frequencies_hz, strict=True)
        )
        row[Ch.GRIP] += self.grip_amplitude * math.sin(math.tau * self.grip_frequency_hz * t)
        return row
