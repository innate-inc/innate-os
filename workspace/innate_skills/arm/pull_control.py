# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""ROS-free control helpers for contact-aware pulling."""

import math
from dataclasses import dataclass

ARM_JOINTS = 5  # exclude j6: its current is the standing grip preload
STEER_STEP_RAD = math.radians(8.0)
MAX_STEER_RAD = math.radians(70.0)


def normalize(x: float, y: float, z: float) -> tuple[float, float, float]:
    length = math.sqrt(x * x + y * y + z * z)
    if not math.isfinite(length) or length < 1e-6:
        raise ValueError("pull direction must be a finite, non-zero vector")
    return x / length, y / length, z / length


def load_delta(effort: tuple[float, ...], baseline: tuple[float, ...]) -> float:
    """Largest arm-joint load change, in percentage points."""
    return max(abs(value - tare) for value, tare in zip(effort[:ARM_JOINTS], baseline, strict=True))


@dataclass
class PullGuidance:
    """Small bounded steering heuristic for following a constrained pull."""

    nominal: tuple[float, float, float]
    soft_delta: float
    offset: float = 0.0
    turn_sign: float = 1.0
    previous_delta: float | None = None

    def command(self, resistance: float) -> tuple[tuple[float, float, float], float]:
        step_scale = 1.0
        horizontal = math.hypot(self.nominal[0], self.nominal[1])
        if resistance > self.soft_delta:
            step_scale = 0.45
            # If the previous steering choice made resistance worse, explore
            # the other side. Every trial remains a tiny forward movement.
            worsened = (
                self.previous_delta is not None
                and self.previous_delta > self.soft_delta
                and resistance >= self.previous_delta - 0.5
            )
            turn = STEER_STEP_RAD
            if worsened:
                self.turn_sign *= -1.0
                turn *= 2.0  # cross from the tried side to the opposite side
            candidate = self.offset + self.turn_sign * turn
            if abs(candidate) > MAX_STEER_RAD:
                self.turn_sign *= -1.0
                candidate = self.offset + self.turn_sign * STEER_STEP_RAD
            self.offset = candidate

        self.previous_delta = resistance
        if horizontal < 1e-6:
            return self.nominal, step_scale

        c, s = math.cos(self.offset), math.sin(self.offset)
        x = c * self.nominal[0] - s * self.nominal[1]
        y = s * self.nominal[0] + c * self.nominal[1]
        return normalize(x, y, self.nominal[2]), step_scale
