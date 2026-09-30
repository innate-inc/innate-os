# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The /localize Trigger's reply text: written by grid_localizer, read back by the relocalize skill.
Pure: no ROS, so a skill can import it without the node's dependencies."""

from __future__ import annotations

import math
import re

LOW_CONFIDENCE = "Localized with LOW confidence"  # reply prefix when another place fits the scan nearly as well
_POSE = re.compile(r"\((-?\d+\.\d+), (-?\d+\.\d+), (-?\d+\.\d+)°\)")


def describe_pose(x: float, y: float, theta: float) -> str:
    return f"({x:.2f}, {y:.2f}, {math.degrees(theta):.1f}°)"


def pose_in(message: str) -> tuple[float, float, float] | None:
    """The (x, y, theta) a reply describes, theta in radians; None when it carries no pose."""
    match = _POSE.search(message)
    if match is None:
        return None
    x, y, degrees = (float(value) for value in match.groups())
    return x, y, math.radians(degrees)
