# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing map-frame pose. ROS-free on purpose."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Pose:
    """The robot's pose on the map, read via ``self.pose`` in skills.

    Unlike ``self.odom`` (odom frame: smooth but drifts and resets every
    boot), this is the active map authority's estimate: AMCL in navigation or
    slam_toolbox's TF-derived ``/mapping_pose`` in either mapping mode. These
    are the coordinates ``navigate_to_position`` targets. ``self.pose`` reads
    None until the active authority has produced a map-frame sample.
    """

    x: float
    """Position along X in meters, map frame."""
    y: float
    """Position along Y in meters, map frame."""
    theta: float
    """Yaw in radians, counter-clockwise positive, wrapped to [-pi, pi]."""
    stamp: float = 0.0
    """Sensor timestamp in seconds (ROS time)."""
    frame_id: str = "map"

    def __post_init__(self):
        # same wrapped-theta contract as Odometry, enforced for hand-built
        # instances too
        if not -math.pi <= self.theta <= math.pi:
            # frozen dataclass: object.__setattr__ bypasses the immutability guard
            object.__setattr__(self, "theta", math.atan2(math.sin(self.theta), math.cos(self.theta)))

    @property
    def theta_degrees(self) -> float:
        """Yaw in degrees, counter-clockwise positive."""
        return math.degrees(self.theta)

    @property
    def position(self) -> tuple[float, float]:
        """(x, y) in meters, map frame."""
        return (self.x, self.y)
