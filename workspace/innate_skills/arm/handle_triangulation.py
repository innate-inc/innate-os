# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pure geometry for monocular handle triangulation."""

import math

from innate.geometry import pixel_ray


def camera_ray_odom(u: float, v: float, head_tilt_deg: float, odom_xyt: tuple[float, float, float]):
    """Head-camera pixel ray expressed in the odom frame."""
    origin, direction = pixel_ray(u, v, head_tilt_deg)
    x, y, yaw = odom_xyt
    c, s = math.cos(yaw), math.sin(yaw)

    def rotate_xy(vector):
        return (c * vector[0] - s * vector[1], s * vector[0] + c * vector[1], vector[2])

    offset = rotate_xy(origin)
    return (x + offset[0], y + offset[1], offset[2]), rotate_xy(direction)


def triangulate_rays(first, second, *, min_angle_deg: float = 3.0, max_gap_m: float = 0.05):
    """Closest-point triangulation of two forward rays.

    Returns ``(midpoint, ray_gap, angle_degrees)``. Raises ``ValueError`` for
    weak geometry, intersections behind either camera, or disagreeing rays.
    """
    o1, d1 = first
    o2, d2 = second
    dot = max(-1.0, min(1.0, sum(d1[i] * d2[i] for i in range(3))))
    angle = math.degrees(math.acos(dot))
    if angle < min_angle_deg:
        raise ValueError(f"triangulation angle too small ({angle:.1f} deg)")

    w0 = tuple(o1[i] - o2[i] for i in range(3))
    b = dot
    d = sum(d1[i] * w0[i] for i in range(3))
    e = sum(d2[i] * w0[i] for i in range(3))
    denominator = 1.0 - b * b
    if denominator <= 1e-6:
        raise ValueError("triangulation rays are parallel")
    t1 = (b * e - d) / denominator
    t2 = (e - b * d) / denominator
    if t1 <= 0.0 or t2 <= 0.0:
        raise ValueError("triangulated handle is behind a camera")

    p1 = tuple(o1[i] + t1 * d1[i] for i in range(3))
    p2 = tuple(o2[i] + t2 * d2[i] for i in range(3))
    gap = math.dist(p1, p2)
    if gap > max_gap_m:
        raise ValueError(f"triangulation rays disagree by {gap:.3f} m")
    point = tuple((p1[i] + p2[i]) / 2.0 for i in range(3))
    return point, gap, angle


def odom_point_to_base(point, odom_xyt):
    """Odom-frame point to the robot's current base_link frame."""
    x, y, yaw = odom_xyt
    dx, dy = point[0] - x, point[1] - y
    c, s = math.cos(yaw), math.sin(yaw)
    return (c * dx + s * dy, -s * dx + c * dy, point[2])
