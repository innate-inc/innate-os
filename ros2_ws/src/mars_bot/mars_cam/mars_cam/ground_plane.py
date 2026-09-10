# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Ground-plane geometry for the depth cloud.

Pure functions — no ROS, no I/O. Given points already expressed in ``base_link``
(x forward, y left, z up, z=0 at the floor), these answer the question the
costmap actually cares about: how far off the floor does the reconstruction put
the floor, and where does that cross the marking threshold.
"""

from dataclasses import dataclass

import numpy as np

from mars_cam.calibration_validation import ErrorStats

# Points this far from the nominal floor are obstacles or ceiling, not floor,
# and would drag a least-squares fit off the surface we are trying to measure.
FLOOR_BAND_M = 0.15

RANGE_BINS_M: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0, 2.5)
LEAK_THRESHOLDS_M: tuple[float, ...] = (0.010, 0.020, 0.050)


@dataclass(frozen=True)
class Corridor:
    """The volume ahead that the robot is about to drive through, in base_link.

    x_max is 1.0 m rather than further out because pitch error grows with range
    and, at the -20 degree head position, 0.25-1.0 m all lands within r_norm 0.31
    of the image centre — inside the region where the pinhole model still fits a
    98 degree lens.
    """

    x_min: float = 0.25
    x_max: float = 1.00
    half_width: float = 0.22
    z_min: float = 0.010
    z_max: float = 0.36

    def mask(self, points: np.ndarray) -> np.ndarray:
        x, y, z = points[:, 0], points[:, 1], points[:, 2]
        return (
            (x >= self.x_min)
            & (x <= self.x_max)
            & (np.abs(y) <= self.half_width)
            & (z >= self.z_min)
            & (z <= self.z_max)
        )

    def footprint_mask(self, points: np.ndarray) -> np.ndarray:
        """In-corridor in x/y, ignoring height — the column the robot drives through."""
        x, y = points[:, 0], points[:, 1]
        return (x >= self.x_min) & (x <= self.x_max) & (np.abs(y) <= self.half_width)


@dataclass(frozen=True)
class PlaneFit:
    """Least-squares ``z = gradient_x*x + gradient_y*y + offset`` through floor points."""

    gradient_x: float
    gradient_y: float
    offset_m: float
    residual: ErrorStats
    num_points: int

    @property
    def pitch_deg(self) -> float:
        """Positive means the reconstructed floor rises with distance ahead."""
        return float(np.degrees(np.arctan(self.gradient_x)))

    @property
    def roll_deg(self) -> float:
        return float(np.degrees(np.arctan(self.gradient_y)))

    def height_at(self, x: float) -> float:
        return self.gradient_x * x + self.offset_m


def fit_floor(points: np.ndarray, band_m: float = FLOOR_BAND_M, trim_sigma: float = 3.0) -> PlaneFit | None:
    """Fit the floor, rejecting outliers once so a stray obstacle cannot tilt it."""
    near_floor = points[np.abs(points[:, 2]) <= band_m]
    if len(near_floor) < 3:
        return None

    fit = _least_squares_plane(near_floor)
    residuals = _vertical_residuals(near_floor, fit)
    spread = float(np.std(residuals))
    if spread > 0.0:
        kept = near_floor[np.abs(residuals) <= trim_sigma * spread]
        if len(kept) >= 3:
            near_floor, fit = kept, _least_squares_plane(kept)

    return PlaneFit(
        gradient_x=fit[0],
        gradient_y=fit[1],
        offset_m=fit[2],
        residual=ErrorStats.of(np.abs(_vertical_residuals(near_floor, fit)) * 1000.0),
        num_points=len(near_floor),
    )


def _least_squares_plane(points: np.ndarray) -> tuple[float, float, float]:
    design = np.column_stack([points[:, 0], points[:, 1], np.ones(len(points))])
    coefficients, *_ = np.linalg.lstsq(design, points[:, 2], rcond=None)
    return float(coefficients[0]), float(coefficients[1]), float(coefficients[2])


def _vertical_residuals(points: np.ndarray, fit: tuple[float, float, float]) -> np.ndarray:
    return points[:, 2] - (fit[0] * points[:, 0] + fit[1] * points[:, 1] + fit[2])


def height_error_by_range(
    points: np.ndarray,
    bins: tuple[float, ...] = RANGE_BINS_M,
    band_m: float = FLOOR_BAND_M,
) -> list[tuple[float, float, ErrorStats]]:
    """Floor height, in mm, binned by forward distance.

    A calibration pitch error shows up here as a trend across bins; noise shows
    up as spread within a bin. Telling those apart is the whole point.
    """
    floor = points[np.abs(points[:, 2]) <= band_m]
    edges = (0.0, *bins)
    rows: list[tuple[float, float, ErrorStats]] = []
    for low, high in zip(edges[:-1], edges[1:]):  # noqa: B905 — fixed-length, always aligned
        selected = floor[(floor[:, 0] >= low) & (floor[:, 0] < high)]
        rows.append((low, high, ErrorStats.of(selected[:, 2] * 1000.0)))
    return rows


def leak_fractions(
    points: np.ndarray,
    thresholds: tuple[float, ...] = LEAK_THRESHOLDS_M,
    band_m: float = FLOOR_BAND_M,
) -> dict[float, float]:
    """Fraction of floor points that would be marked as obstacles at each threshold."""
    floor = points[np.abs(points[:, 2]) <= band_m]
    if len(floor) == 0:
        return {t: 0.0 for t in thresholds}
    return {t: float(np.mean(floor[:, 2] > t)) for t in thresholds}


def transform_to_base(points: np.ndarray, rotation: np.ndarray, translation: np.ndarray) -> np.ndarray:
    return points @ rotation.T + translation


def optical_mount_rotation(pitch_deg: float, roll_deg: float) -> np.ndarray:
    """Mirror of ``updateCloudRotation``'s mount fix, in the optical frame.

    Optical axes are x right, y down, z forward, so pitch is a rotation about x
    and roll about z. Kept in step with the C++ so the sign convention has one
    executable definition rather than two prose descriptions.
    """
    p, r = np.radians(pitch_deg), np.radians(roll_deg)
    pitch = np.array([[1.0, 0.0, 0.0], [0.0, np.cos(p), -np.sin(p)], [0.0, np.sin(p), np.cos(p)]])
    roll = np.array([[np.cos(r), -np.sin(r), 0.0], [np.sin(r), np.cos(r), 0.0], [0.0, 0.0, 1.0]])
    return roll @ pitch


def mount_correction_for(fit: PlaneFit) -> tuple[float, float]:
    """The mount_pitch/roll_correction_deg that flattens a measured floor.

    An optical-frame tilt of e degrees shows up as a base_link floor slope of
    exactly e, so the correction is its negation.
    """
    return -fit.pitch_deg, -fit.roll_deg


@dataclass(frozen=True)
class Intrinsics:
    fx: float
    fy: float
    cx: float
    cy: float
    width: int
    height: int


def image_radius(
    points_base: np.ndarray,
    rotation: np.ndarray,
    translation: np.ndarray,
    intrinsics: Intrinsics,
) -> np.ndarray:
    """Where base_link points land in the image, as radius from centre over half-diagonal.

    Answers whether the corridor is being sampled through the middle of the lens
    or its edge — the edge is where a 5-parameter distortion model on a ~98
    degree lens stops fitting, so it is where depth is least trustworthy.
    """
    camera = (points_base - translation) @ rotation
    forward = camera[:, 2]
    visible = forward > 1e-6
    if not np.any(visible):
        return np.empty(0)

    camera = camera[visible]
    u = intrinsics.fx * camera[:, 0] / camera[:, 2] + intrinsics.cx
    v = intrinsics.fy * camera[:, 1] / camera[:, 2] + intrinsics.cy
    half_diagonal = 0.5 * float(np.hypot(intrinsics.width, intrinsics.height))
    return np.hypot(u - intrinsics.width / 2.0, v - intrinsics.height / 2.0) / half_diagonal


def quaternion_matrix(x: float, y: float, z: float, w: float) -> np.ndarray:
    """Rotation matrix from a ROS quaternion, without pulling in tf_transformations."""
    norm = np.sqrt(x * x + y * y + z * z + w * w)
    if norm == 0.0:
        return np.eye(3)
    x, y, z, w = x / norm, y / norm, z / norm, w / norm
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )
