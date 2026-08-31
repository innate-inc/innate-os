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


def validate_vertical_box(box, *, min_height_px: float = 24.0, edge_margin_px: float = 4.0):
    """Validate a tight ``(x, y, width, height)`` box for a vertical handle.

    Known-size ranging is only valid when both physical endpoints are visible.
    A clipped, squat, or tiny box is therefore rejected instead of producing a
    confidently wrong range.
    """
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError("handle detection did not contain a bounding box")
    x, y, width, height = (float(value) for value in box)
    if not all(math.isfinite(value) for value in (x, y, width, height)):
        raise ValueError("handle bounding box is invalid")
    if height < min_height_px:
        raise ValueError(f"handle is too small for metric ranging ({height:.0f} px tall)")
    if width <= 0.0 or height < 1.5 * width:
        raise ValueError("detected handle is not a vertical component")
    clipped = (
        x < edge_margin_px
        or y < edge_margin_px
        or x + width > 640.0 - edge_margin_px
        or y + height > 480.0 - edge_margin_px
    )
    if clipped:
        raise ValueError("the full top and bottom of the handle are not visible")
    return x, y, width, height


def stable_vertical_box(
    boxes,
    *,
    max_height_spread_fraction: float = 0.15,
    max_center_spread_px: float = 24.0,
):
    """Median box from repeated observations, rejecting unstable tracking."""
    if len(boxes) < 3:
        raise ValueError("fewer than three valid handle observations")
    valid = [validate_vertical_box(box) for box in boxes]
    heights = [box[3] for box in valid]
    centers = [(box[0] + box[2] / 2.0, box[1] + box[3] / 2.0) for box in valid]
    median_height = sorted(heights)[len(heights) // 2]
    if (max(heights) - min(heights)) / median_height > max_height_spread_fraction:
        raise ValueError("handle size estimate is unstable")
    median_u = sorted(center[0] for center in centers)[len(centers) // 2]
    median_v = sorted(center[1] for center in centers)[len(centers) // 2]
    if max(math.hypot(center[0] - median_u, center[1] - median_v) for center in centers) > max_center_spread_px:
        raise ValueError("handle position estimate is unstable")
    median_width = sorted(box[2] for box in valid)[len(valid) // 2]
    return median_u - median_width / 2.0, median_v - median_height / 2.0, median_width, median_height


def vertical_handle_target(box, physical_height_m, *, fx, fy, cx, cy, camera_origin):
    """Project a known-height vertical box into ``base_link``.

    The camera is assumed level and forward-facing.  The returned optical
    range is the distance along its forward axis, not Euclidean range.
    """
    x, y, width, height = validate_vertical_box(box)
    if not math.isfinite(physical_height_m) or physical_height_m <= 0.0:
        raise ValueError("physical handle height must be positive")
    if min(fx, fy) <= 0.0:
        raise ValueError("camera focal lengths must be positive")
    optical_range = fy * physical_height_m / height
    u, v = x + width / 2.0, y + height / 2.0
    return (
        camera_origin[0] + optical_range,
        camera_origin[1] - (u - cx) * optical_range / fx,
        camera_origin[2] - (v - cy) * optical_range / fy,
    ), optical_range


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
