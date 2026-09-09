# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pinhole geometry for the people engine: pixels <-> the floor, and the
published 640x480 frame <-> the camera's native 1280x720 left eye.

PURE module: math only, no ROS, no cv2.

The driver squashes the native 1280x720 left eye to 640x480 anisotropically
(x0.5 horizontally, x0.667 vertically), so the published intrinsics have
FX != FY and ``K_native = diag(2, 1.5, 1) . K_pub``. Boxes everywhere are the
normalized ``(ymin, xmin, ymax, xmax)`` of the published frame, which is
resolution-independent; only crops ever speak pixels.

Head pitch follows the /mars/head convention: degrees, negative = looking down.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from brain_client.people.types import Box

PUBLISHED_WIDTH, PUBLISHED_HEIGHT = 640, 480
NATIVE_WIDTH, NATIVE_HEIGHT = 1280, 720
NATIVE_SCALE_X = NATIVE_WIDTH / PUBLISHED_WIDTH  # 2.0
NATIVE_SCALE_Y = NATIVE_HEIGHT / PUBLISHED_HEIGHT  # 1.5

# Left-eye intrinsics of the published frame, tape-measured 2026-08-28 and
# consistent with the factory native focal of ~400.8 px at 1280x720
# (innate/geometry.py). Used when CameraInfo carries an all-zero K.
FX, FY = 200.3, 267.3
CX, CY = 319.1, 248.7
NATIVE_FOCAL_PX = 400.8

# URDF: base_footprint is the floor, the head joint sits at z = 0.2588 and the
# camera 0.0003 below it inside the head. Tape-measure before trusting.
CAMERA_HEIGHT_M = 0.26

# Beyond this the floor ray grazes and a pixel of box-bottom error is a metre
# of range error (the same cap brain/grounding.py uses).
MAX_FLOOR_RANGE_M = 3.5

# A box whose bottom sits this close to the frame edge has its feet cut off, so
# the floor ray reads the crop edge rather than the ground contact point.
FEET_CUTOFF = 0.98

# Faces live in the top of a person box; YuNet runs on this fraction of it.
HEAD_REGION_FRACTION = 0.4


def published_to_native(x: float, y: float) -> tuple[float, float]:
    return (x * NATIVE_SCALE_X, y * NATIVE_SCALE_Y)


def native_to_published(x: float, y: float) -> tuple[float, float]:
    return (x / NATIVE_SCALE_X, y / NATIVE_SCALE_Y)


def box_center(box: Box) -> tuple[float, float]:
    ymin, xmin, ymax, xmax = box
    return ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)


def box_size(box: Box) -> tuple[float, float]:
    ymin, xmin, ymax, xmax = box
    return (max(0.0, xmax - xmin), max(0.0, ymax - ymin))


def head_region(box: Box, fraction: float = HEAD_REGION_FRACTION) -> Box:
    """The upper slice of a person box, where the face is."""
    ymin, xmin, ymax, xmax = box
    return (ymin, xmin, ymin + (ymax - ymin) * fraction, xmax)


def feet_visible(box: Box) -> bool:
    return box[2] < FEET_CUTOFF


def pose_from_landmarks(landmarks: Sequence[tuple[float, float]]) -> tuple[float, float]:
    """Coarse (yaw, pitch) in degrees from a locator's five points — left eye,
    right eye, nose, left mouth, right mouth. Positive yaw is turned toward the
    image right; positive pitch is the face seen from below, the pose a
    knee-high robot gets by default and the one ArcFace-class models handle
    worst. This is a pose bucket, not a head-pose model, and a locator that
    reports no landmarks reads as frontal.
    """
    if len(landmarks) < 5:
        return (0.0, 0.0)
    (lx, ly), (rx, ry), (nx, ny), (mlx, mly), (mrx, mry) = landmarks[:5]
    eye_x, eye_y = (lx + rx) / 2.0, (ly + ry) / 2.0
    mouth_x, mouth_y = (mlx + mrx) / 2.0, (mly + mry) / 2.0
    eye_span = math.hypot(rx - lx, ry - ly)
    if eye_span < 1e-6:
        return (0.0, 0.0)
    yaw = max(-90.0, min(90.0, 90.0 * (nx - eye_x) / eye_span))
    face_span = math.hypot(mouth_x - eye_x, mouth_y - eye_y)
    if face_span < 1e-6:
        return (yaw, 0.0)
    along = ((nx - eye_x) * (mouth_x - eye_x) + (ny - eye_y) * (mouth_y - eye_y)) / face_span**2
    return (yaw, max(-90.0, min(90.0, 180.0 * (along - 0.5))))


@dataclass(frozen=True)
class CameraModel:
    """Intrinsics of one rectilinear image plus the lens height above the floor."""

    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int
    distortion: tuple[float, ...] = ()
    camera_height_m: float = CAMERA_HEIGHT_M

    @classmethod
    def published_default(cls, camera_height_m: float = CAMERA_HEIGHT_M) -> CameraModel:
        return cls(FX, FY, CX, CY, PUBLISHED_WIDTH, PUBLISHED_HEIGHT, (), camera_height_m)

    @classmethod
    def from_camera_info(
        cls,
        k: Sequence[float] | None,
        width: int,
        height: int,
        distortion: Sequence[float] = (),
        camera_height_m: float = CAMERA_HEIGHT_M,
    ) -> CameraModel:
        """A CameraInfo's K (row-major 3x3) as a model. An uncalibrated driver
        publishes an all-zero K, which falls back to the tape-measured defaults
        scaled to the frame size given."""
        if k is None or len(k) < 9 or k[0] <= 0.0 or k[4] <= 0.0:
            sx, sy = width / PUBLISHED_WIDTH, height / PUBLISHED_HEIGHT
            return cls(FX * sx, FY * sy, CX * sx, CY * sy, width, height, tuple(distortion), camera_height_m)
        return cls(k[0], k[4], k[2], k[5], width, height, tuple(distortion), camera_height_m)

    def native(self) -> CameraModel:
        """The same lens seen at the sensor's own 1280x720: ``diag(2, 1.5, 1).K``.
        Distortion coefficients are dimensionless and carry over unchanged."""
        return CameraModel(
            self.fx * NATIVE_SCALE_X,
            self.fy * NATIVE_SCALE_Y,
            self.cx * NATIVE_SCALE_X,
            self.cy * NATIVE_SCALE_Y,
            round(self.width * NATIVE_SCALE_X),
            round(self.height * NATIVE_SCALE_Y),
            self.distortion,
            self.camera_height_m,
        )

    def pixel_of(self, x_norm: float, y_norm: float) -> tuple[float, float]:
        return (x_norm * self.width, y_norm * self.height)

    def ray(self, u: float, v: float, head_pitch_deg: float) -> tuple[float, float, float]:
        """Unit-forward ray through pixel (u, v) in the robot frame (x forward,
        y left, z up) for a head pitched ``head_pitch_deg`` (negative = down)."""
        xo, yo = (u - self.cx) / self.fx, (v - self.cy) / self.fy
        theta = math.radians(head_pitch_deg)
        cos_t, sin_t = math.cos(theta), math.sin(theta)
        # Optical axis pitched about Y; image right is -y, image down is the
        # pitched -z. Same rotation as innate/geometry._cam_pose.
        return (cos_t + yo * sin_t, -xo, sin_t - yo * cos_t)

    def bearing_deg(self, box: Box, head_pitch_deg: float = 0.0) -> float:
        """Horizontal angle to the box centre; positive = left of the robot."""
        cx_norm, cy_norm = box_center(box)
        u, v = self.pixel_of(cx_norm, cy_norm)
        dx, dy, _ = self.ray(u, v, head_pitch_deg)
        return math.degrees(math.atan2(dy, dx))

    def floor_range(self, box: Box, head_pitch_deg: float = 0.0) -> float | None:
        """Ground range to the person whose box bottom is their floor contact,
        capped at :data:`MAX_FLOOR_RANGE_M`; None when the ray never reaches the
        floor or the feet are cut off by the frame edge."""
        if not feet_visible(box):
            return None
        cx_norm, _ = box_center(box)
        u, v = self.pixel_of(cx_norm, box[2])
        dx, dy, dz = self.ray(u, v, head_pitch_deg)
        if dz >= -1e-6:
            return None
        scale = self.camera_height_m / -dz
        return min(math.hypot(scale * dx, scale * dy), MAX_FLOOR_RANGE_M)

    def height_from_range(self, box: Box, range_m: float, head_pitch_deg: float = 0.0) -> float | None:
        """Standing height from the head-top row of a box at a known ground range."""
        if range_m <= 0.0:
            return None
        cx_norm, _ = box_center(box)
        u, v = self.pixel_of(cx_norm, box[0])
        dx, dy, dz = self.ray(u, v, head_pitch_deg)
        ground = math.hypot(dx, dy)
        if ground < 1e-6:
            return None
        return self.camera_height_m + range_m * dz / ground

    def range_from_height(self, box: Box, height_m: float, head_pitch_deg: float = 0.0) -> float | None:
        """Ground range implied by a known standing height — the fallback when
        the feet are out of frame."""
        cx_norm, _ = box_center(box)
        u, v = self.pixel_of(cx_norm, box[0])
        dx, dy, dz = self.ray(u, v, head_pitch_deg)
        ground = math.hypot(dx, dy)
        rise = height_m - self.camera_height_m
        if ground < 1e-6 or dz <= 1e-6 or rise <= 0.0:
            return None
        return min(rise * ground / dz, MAX_FLOOR_RANGE_M)

    def box_pixels(self, box: Box) -> tuple[int, int, int, int]:
        """``(x0, y0, x1, y1)`` of a normalized box in this model's pixels."""
        ymin, xmin, ymax, xmax = box
        return (
            round(xmin * self.width),
            round(ymin * self.height),
            round(xmax * self.width),
            round(ymax * self.height),
        )

    def box_height_px(self, box: Box) -> float:
        return box_size(box)[1] * self.height
