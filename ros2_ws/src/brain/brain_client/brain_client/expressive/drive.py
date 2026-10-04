# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Base tracking: turn the orient/advance offsets of an actuator pose into a differential-drive twist,
from odometry relative to the anchor where expression started."""

from __future__ import annotations

import math
from dataclasses import dataclass

Pose2D = tuple[float, float, float]


def _wrap(angle: float) -> float:
    return math.atan2(math.sin(angle), math.cos(angle))


@dataclass(frozen=True)
class BaseTracker:
    """P-control on the anchor-frame error plus the target's own velocity as feed-forward.

    The base cannot move sideways, so the advance error is projected onto the current heading.
    """

    kp_x: float = 3.0
    kp_yaw: float = 4.0
    max_vx: float = 0.3
    max_wz: float = 1.5
    deadband_x: float = 0.005
    deadband_yaw: float = 0.01

    def twist(
        self,
        anchor: Pose2D,
        odom: Pose2D,
        base_x: float,
        base_yaw: float,
        velocity_x: float = 0.0,
        velocity_yaw: float = 0.0,
    ) -> tuple[float, float]:
        """``(vx, wz)`` that brings ``odom`` to ``anchor`` offset by ``base_x`` metres and ``base_yaw`` rad."""
        ax, ay, ayaw = anchor
        x, y, yaw = odom
        target_x = ax + base_x * math.cos(ayaw)
        target_y = ay + base_x * math.sin(ayaw)
        along = (target_x - x) * math.cos(yaw) + (target_y - y) * math.sin(yaw)
        yaw_error = _wrap(ayaw + base_yaw - yaw)
        vx = velocity_x * math.cos(yaw - ayaw) + (self.kp_x * along if abs(along) > self.deadband_x else 0.0)
        wz = velocity_yaw + (self.kp_yaw * yaw_error if abs(yaw_error) > self.deadband_yaw else 0.0)
        return max(-self.max_vx, min(self.max_vx, vx)), max(-self.max_wz, min(self.max_wz, wz))
