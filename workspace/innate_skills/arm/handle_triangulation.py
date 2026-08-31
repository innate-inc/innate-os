# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pure geometry for monocular handle triangulation."""

import math

from innate.geometry import pixel_ray, pixel_to_floor


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


def handle_from_floor_edge(handle_px, left_floor_px, right_floor_px, head_tilt_deg):
    """Intersect a handle pixel ray with the vertical plane above a floor edge.

    The two edge pixels must identify separated points on the same straight
    cabinet/door front where it meets the floor. Returns
    ``(handle_xyz, left_xy, right_xy, plane_yaw)`` in ``base_link``.
    """
    left = pixel_to_floor(*left_floor_px, head_tilt_deg)
    right = pixel_to_floor(*right_floor_px, head_tilt_deg)
    if left is None or right is None:
        raise ValueError("cabinet floor corners do not project onto the floor")
    edge = (right[0] - left[0], right[1] - left[1])
    width = math.hypot(*edge)
    if width < 0.15:
        raise ValueError(f"cabinet floor edge is too short ({width:.2f} m)")

    normal = (-edge[1] / width, edge[0] / width)
    origin, direction = pixel_ray(*handle_px, head_tilt_deg)
    denominator = normal[0] * direction[0] + normal[1] * direction[1]
    if abs(denominator) < 1e-4:
        raise ValueError("handle ray is parallel to the cabinet plane")
    distance = (normal[0] * (left[0] - origin[0]) + normal[1] * (left[1] - origin[1])) / denominator
    if distance <= 0.0:
        raise ValueError("cabinet plane is behind the camera")
    point = tuple(origin[i] + distance * direction[i] for i in range(3))
    return point, left, right, math.atan2(edge[1], edge[0])
