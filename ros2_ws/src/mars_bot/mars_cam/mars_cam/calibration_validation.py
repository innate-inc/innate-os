# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Held-out validation metrics for a solved stereo calibration.

Pure functions — no ROS, no file I/O. Everything here runs on image pairs that
were deliberately kept out of ``calibrateCamera``/``stereoCalibrate``, so the
numbers measure generalisation rather than fit.
"""

from dataclasses import asdict, dataclass, field

import cv2
import numpy as np

from mars_cam.checkerboard import PatternSize

# Normalised radius bin edges, in units of half the image diagonal.
RADIAL_EDGES: tuple[float, float] = (1.0 / 3.0, 2.0 / 3.0)
RADIAL_LABELS: tuple[str, str, str] = ("center", "middle", "outer")


@dataclass(frozen=True)
class ErrorStats:
    """The distribution summary reported for every error metric."""

    count: int
    mean: float
    rms: float
    median: float
    p95: float
    maximum: float

    @staticmethod
    def of(values: np.ndarray) -> "ErrorStats":
        flat = np.asarray(values, dtype=np.float64).ravel()
        if flat.size == 0:
            return ErrorStats(0, 0.0, 0.0, 0.0, 0.0, 0.0)
        return ErrorStats(
            count=int(flat.size),
            mean=float(np.mean(flat)),
            rms=float(np.sqrt(np.mean(flat**2))),
            median=float(np.median(flat)),
            p95=float(np.percentile(flat, 95)),
            maximum=float(np.max(flat)),
        )


@dataclass(frozen=True)
class StereoCalibrationMatrices:
    """The solved parameters the validator needs, in OpenCV conventions."""

    K1: np.ndarray
    D1: np.ndarray
    K2: np.ndarray
    D2: np.ndarray
    R1: np.ndarray
    R2: np.ndarray
    P1: np.ndarray
    P2: np.ndarray


@dataclass(frozen=True)
class ValidationPair:
    """One held-out stereo observation of the target."""

    index: int
    object_points: np.ndarray
    corners_left: np.ndarray
    corners_right: np.ndarray
    grid_shape: PatternSize | None = None


@dataclass(frozen=True)
class PerImageMetrics:
    index: int
    left_reprojection: ErrorStats
    right_reprojection: ErrorStats
    epipolar_dy: ErrorStats
    spacing_error_mm: ErrorStats
    planarity_rms_mm: float
    planarity_p95_mm: float
    median_depth_m: float


@dataclass(frozen=True)
class RadialBin:
    label: str
    r_min: float
    r_max: float
    reprojection: ErrorStats
    epipolar_dy: ErrorStats


@dataclass(frozen=True)
class ValidationReport:
    """Everything measured on the held-out set, ready to serialise."""

    num_pairs: int
    left_reprojection: ErrorStats
    right_reprojection: ErrorStats
    epipolar_dy: ErrorStats
    spacing_error_mm: ErrorStats
    mean_spacing_mm: float
    nominal_spacing_mm: float
    planarity_rms_mm: ErrorStats
    planarity_p95_mm: ErrorStats
    median_depth_m: float
    q_yields_positive_z: bool
    radial_bins: list[RadialBin]
    per_image: list[PerImageMetrics]
    corner_samples: list[dict[str, float]] = field(default_factory=list, repr=False)

    def to_dict(self) -> dict:
        return asdict(self)


def _reprojection_errors(pair_obj: np.ndarray, corners: np.ndarray, K: np.ndarray, D: np.ndarray) -> np.ndarray:
    """Per-corner reprojection error after fitting this image's pose alone.

    The board pose is not a calibration output for a held-out image, so it has
    to be recovered with solvePnP before the intrinsics can be scored.
    """
    obj = np.asarray(pair_obj, dtype=np.float64).reshape(-1, 1, 3)
    img = np.asarray(corners, dtype=np.float64).reshape(-1, 1, 2)
    ok, rvec, tvec = cv2.solvePnP(obj, img, K, D, flags=cv2.SOLVEPNP_ITERATIVE)
    if not ok:
        return np.empty(0)
    projected, _ = cv2.projectPoints(obj, rvec, tvec, K, D)
    return np.linalg.norm(projected.reshape(-1, 2) - img.reshape(-1, 2), axis=1)


def _rectify(corners: np.ndarray, K: np.ndarray, D: np.ndarray, R: np.ndarray, P: np.ndarray) -> np.ndarray:
    return cv2.undistortPoints(np.asarray(corners, dtype=np.float64).reshape(-1, 1, 2), K, D, R=R, P=P).reshape(-1, 2)


def _triangulate(rect_left: np.ndarray, rect_right: np.ndarray, P1: np.ndarray, P2: np.ndarray) -> np.ndarray:
    homogeneous = cv2.triangulatePoints(P1, P2, rect_left.T.copy(), rect_right.T.copy())
    w = homogeneous[3]
    w[np.abs(w) < 1e-12] = 1e-12
    return (homogeneous[:3] / w).T


def _neighbour_spacings(points: np.ndarray, grid_shape: PatternSize) -> np.ndarray:
    """Distances between 4-neighbour corners of the reconstructed grid."""
    cols, rows = grid_shape
    grid = points.reshape(rows, cols, 3)
    horizontal = np.linalg.norm(grid[:, 1:] - grid[:, :-1], axis=2).ravel()
    vertical = np.linalg.norm(grid[1:] - grid[:-1], axis=2).ravel()
    return np.concatenate([horizontal, vertical])


def _plane_residuals(points: np.ndarray) -> np.ndarray:
    """Point-to-plane distances for the best-fit plane through the corners."""
    if len(points) < 3:
        return np.empty(0)
    centred = points - points.mean(axis=0)
    _, _, vt = np.linalg.svd(centred, full_matrices=False)
    return np.abs(centred @ vt[2])


def _radial_norm(corners: np.ndarray, image_size: tuple[int, int]) -> np.ndarray:
    """Distance from the image centre, normalised by half the image diagonal."""
    width, height = image_size
    centre = np.array([width / 2.0, height / 2.0])
    half_diagonal = 0.5 * float(np.hypot(width, height))
    return np.linalg.norm(corners.reshape(-1, 2) - centre, axis=1) / half_diagonal


def evaluate(
    calib: StereoCalibrationMatrices,
    pairs: list[ValidationPair],
    image_size: tuple[int, int],
    square_size: float,
) -> ValidationReport:
    """Score a solved calibration on stereo pairs it never saw."""
    per_image: list[PerImageMetrics] = []
    all_left: list[np.ndarray] = []
    all_right: list[np.ndarray] = []
    all_dy: list[np.ndarray] = []
    all_spacing_mm: list[np.ndarray] = []
    all_spacing_error_mm: list[np.ndarray] = []
    all_planarity_rms: list[float] = []
    all_planarity_p95: list[float] = []
    all_depths: list[np.ndarray] = []
    radii: list[np.ndarray] = []
    corner_samples: list[dict[str, float]] = []

    for pair in pairs:
        left_err = _reprojection_errors(pair.object_points, pair.corners_left, calib.K1, calib.D1)
        right_err = _reprojection_errors(pair.object_points, pair.corners_right, calib.K2, calib.D2)

        rect_left = _rectify(pair.corners_left, calib.K1, calib.D1, calib.R1, calib.P1)
        rect_right = _rectify(pair.corners_right, calib.K2, calib.D2, calib.R2, calib.P2)
        dy = np.abs(rect_left[:, 1] - rect_right[:, 1])

        points3d = _triangulate(rect_left, rect_right, calib.P1, calib.P2)
        depths = points3d[:, 2]

        # A wrong-signed P2 negates the whole reconstruction, which leaves every
        # distance intact — so spacing and planarity stay meaningful and only
        # the depth sign reports the problem.
        spacing = _neighbour_spacings(points3d, pair.grid_shape) if pair.grid_shape is not None else np.empty(0)
        spacing_mm = spacing * 1000.0
        spacing_error_mm = np.abs(spacing - square_size) * 1000.0
        planarity_mm = _plane_residuals(points3d) * 1000.0
        planarity_rms = float(np.sqrt(np.mean(planarity_mm**2))) if planarity_mm.size else 0.0
        planarity_p95 = float(np.percentile(planarity_mm, 95)) if planarity_mm.size else 0.0

        radius = _radial_norm(pair.corners_left, image_size)
        radii.append(radius)
        for i in range(len(radius)):
            corner_samples.append(
                {
                    "image_index": float(pair.index),
                    "corner_index": float(i),
                    "u_left": float(pair.corners_left.reshape(-1, 2)[i, 0]),
                    "v_left": float(pair.corners_left.reshape(-1, 2)[i, 1]),
                    "radius_norm": float(radius[i]),
                    "reprojection_px": float(left_err[i]) if i < len(left_err) else float("nan"),
                    "epipolar_dy_px": float(dy[i]),
                }
            )

        all_left.append(left_err)
        all_right.append(right_err)
        all_dy.append(dy)
        all_spacing_mm.append(spacing_mm)
        all_spacing_error_mm.append(spacing_error_mm)
        all_planarity_rms.append(planarity_rms)
        all_planarity_p95.append(planarity_p95)
        all_depths.append(depths)

        per_image.append(
            PerImageMetrics(
                index=pair.index,
                left_reprojection=ErrorStats.of(left_err),
                right_reprojection=ErrorStats.of(right_err),
                epipolar_dy=ErrorStats.of(dy),
                spacing_error_mm=ErrorStats.of(spacing_error_mm),
                planarity_rms_mm=planarity_rms,
                planarity_p95_mm=planarity_p95,
                median_depth_m=float(np.median(depths)),
            )
        )

    spacing_mm_all = _concat(all_spacing_mm)
    depth_all = _concat(all_depths)
    median_depth = float(np.median(depth_all)) if depth_all.size else 0.0

    return ValidationReport(
        num_pairs=len(pairs),
        left_reprojection=ErrorStats.of(_concat(all_left)),
        right_reprojection=ErrorStats.of(_concat(all_right)),
        epipolar_dy=ErrorStats.of(_concat(all_dy)),
        spacing_error_mm=ErrorStats.of(_concat(all_spacing_error_mm)),
        mean_spacing_mm=float(np.mean(spacing_mm_all)) if spacing_mm_all.size else 0.0,
        nominal_spacing_mm=square_size * 1000.0,
        planarity_rms_mm=ErrorStats.of(np.array(all_planarity_rms)),
        planarity_p95_mm=ErrorStats.of(np.array(all_planarity_p95)),
        median_depth_m=median_depth,
        q_yields_positive_z=median_depth > 0.0,
        radial_bins=_bin_by_radius(_concat(radii), _concat(all_left), _concat(all_dy)),
        per_image=per_image,
        corner_samples=corner_samples,
    )


def _concat(arrays: list[np.ndarray]) -> np.ndarray:
    usable = [a for a in arrays if a.size]
    return np.concatenate(usable) if usable else np.empty(0)


def _bin_by_radius(radii: np.ndarray, reprojection: np.ndarray, dy: np.ndarray) -> list[RadialBin]:
    """Split corner errors into centre/middle/outer rings of the image."""
    if radii.size == 0:
        return []

    def masked(values: np.ndarray, mask: np.ndarray) -> ErrorStats:
        # solvePnP can drop an image, leaving reprojection shorter than radii.
        return ErrorStats.of(values[mask]) if values.size == radii.size else ErrorStats.of(np.empty(0))

    edges = (0.0, RADIAL_EDGES[0], RADIAL_EDGES[1], float("inf"))
    bins: list[RadialBin] = []
    for label, low, high in zip(RADIAL_LABELS, edges[:-1], edges[1:]):  # noqa: B905 — fixed-length, always aligned
        mask = (radii >= low) & (radii < high)
        bins.append(
            RadialBin(
                label=label,
                r_min=low,
                r_max=high,
                reprojection=masked(reprojection, mask),
                epipolar_dy=masked(dy, mask),
            )
        )
    return bins
