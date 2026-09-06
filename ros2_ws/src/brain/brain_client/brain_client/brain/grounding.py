# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Visual navigation grounding: a pointed-at pixel -> a floor target.

The model points at a spot in the camera frame (normalized 0-1000 image
coordinates, the convention Gemini uses natively); this module casts that
pixel's ray through the calibrated camera geometry and intersects it with the
ground plane, yielding a robot-frame (x forward, y left) navigation target.

The pixel<->angle mapping is the same linear approximation the cloud brain's
``projection_utils`` used for its candidate-point markers (angle proportional
to pixel offset), with the identical camera model: mounted ``cam_forward``
ahead of base_link at ``cam_height``, pitched about Y by the head angle
(degrees, negative = looking down — the /mars/head convention).

PURE module: math only, no ROS. The actual frame aspect ratio is read from the
JPEG header rather than trusted from config, because the camera driver's
output size can differ from the calibrated nominal.
"""

from __future__ import annotations

import math

# Beyond this the ray grazes the floor and centimeter-level pixel error turns
# into meters of range error; the target is capped and approached in steps.
MAX_RANGE_M = 3.5

# Stop short of the pointed spot so the robot ends up facing it, not on it.
STANDOFF_M = 0.35


def jpeg_dimensions(jpeg: bytes) -> tuple[int, int] | None:
    """Read (width, height) from a JPEG's SOF marker without decoding pixels."""
    if len(jpeg) < 2 or jpeg[0] != 0xFF or jpeg[1] != 0xD8:
        return None
    i, n = 2, len(jpeg)
    while i < n:
        if jpeg[i] != 0xFF:
            i += 1
            continue
        while i < n and jpeg[i] == 0xFF:  # skip fill bytes before the marker
            i += 1
        if i >= n:
            break
        marker = jpeg[i]
        i += 1
        # Standalone markers (SOI/EOI/TEM/RSTn) carry no length field.
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if i + 1 >= n:
            break
        # SOF markers (0xC0-0xCF, except DHT/JPG/DAC) hold the frame size.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 6 >= n:
                break
            height = (jpeg[i + 3] << 8) | jpeg[i + 4]
            width = (jpeg[i + 5] << 8) | jpeg[i + 6]
            return (width, height)
        i += (jpeg[i] << 8) | jpeg[i + 1]  # skip this marker segment
    return None


def pixel_to_floor(
    u_norm: float,
    v_norm: float,
    *,
    frame_jpeg: bytes,
    vertical_fov_deg: float,
    pitch_deg: float,
    cam_height: float,
    cam_forward: float,
) -> tuple[float, float] | None:
    """Project a pointed-at pixel onto the floor.

    Args:
        u_norm, v_norm: normalized image coordinates, 0-1000 from left / from top.
        frame_jpeg: the exact frame the coordinates refer to (for its aspect ratio).
        vertical_fov_deg: camera vertical field of view.
        pitch_deg: head pitch at capture time (degrees, negative = down).
        cam_height / cam_forward: camera mount relative to base_link (meters).

    Returns:
        (x forward, y left) in the robot frame at capture time, range-capped to
        :data:`MAX_RANGE_M`; None when the pixel is at or above the horizon.
    """
    dims = jpeg_dimensions(frame_jpeg)
    aspect = (dims[0] / dims[1]) if dims else 4.0 / 3.0
    v_fov = math.radians(vertical_fov_deg)
    h_fov = 2.0 * math.atan(math.tan(v_fov / 2.0) * aspect)

    # Pixel offset from image center -> view angles (positive = left / up).
    h_ang = (500.0 - u_norm) / 1000.0 * h_fov
    v_ang = (500.0 - v_norm) / 1000.0 * v_fov

    # Ray in the camera frame (x forward, y left, z up), then pitch about Y
    # into the robot frame. Same rotation as the cloud's ground-plane model:
    # theta = -pitch, so a downward-looking head tilts the ray toward the floor.
    dx_cam, dy, dz_cam = 1.0, math.tan(h_ang), math.tan(v_ang)
    theta = math.radians(-pitch_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    dx = cos_t * dx_cam + sin_t * dz_cam
    dz = -sin_t * dx_cam + cos_t * dz_cam

    if dz >= -1e-6:
        return None  # at or above the horizon: the ray never reaches the floor

    scale = cam_height / -dz
    x = cam_forward + scale * dx
    y = scale * dy

    distance = math.hypot(x, y)
    if distance > MAX_RANGE_M:
        x *= MAX_RANGE_M / distance
        y *= MAX_RANGE_M / distance
    return (x, y)


def parse_standoff(value: object = STANDOFF_M) -> float | None:
    """Only bounded numeric distances; bool is not a distance."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not STANDOFF_M <= value <= 1.5 or not math.isfinite(value):
        return None
    return float(value)


def conversation_floor(
    u_norm: float, v_norm: float, *, frame_jpeg: bytes, pitch_deg: float
) -> tuple[float, float] | None:
    """Calibrated main-camera ray, retaining true range until goal construction.

    The pitch-only URDF head has no yaw; base yaw is handled by pose rebasing.
    Intrinsics describe the full image resized to 640x480, not a cropped image.
    Geometry cannot determine whether a selected pixel actually shows feet.
    """
    from innate import geometry

    dims = jpeg_dimensions(frame_jpeg)
    if not dims or min(dims) <= 0 or not math.isfinite(pitch_deg):
        return None
    floor = geometry.pixel_to_floor(u_norm * geometry.IMG_W / 1000, v_norm * geometry.IMG_H / 1000, pitch_deg)
    return floor if floor is not None and all(math.isfinite(v) for v in floor) else None


def approach_goal(floor_x: float, floor_y: float, standoff_m: float = STANDOFF_M) -> dict:
    """Stop short, facing the spot; explicit conversation goals have bounded travel."""
    distance = math.hypot(floor_x, floor_y)
    travel = max(distance - standoff_m, 0.0)
    if standoff_m != STANDOFF_M:
        travel = min(travel, MAX_RANGE_M - standoff_m)
    ratio = travel / distance if distance > 1e-6 else 0.0
    heading = math.atan2(floor_y, floor_x)
    return {
        "x": floor_x * ratio,
        "y": floor_y * ratio,
        "theta_degrees": math.degrees(heading),
        "local_frame": True,
    }
