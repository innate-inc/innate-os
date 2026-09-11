# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Plain-checkerboard detection and per-capture quality diagnostics.

Pure helpers — no ROS, no file I/O — so the stereo calibrator's checkerboard
mode and its offline validation pass share one detector.
"""

from dataclasses import dataclass, field

import cv2
import numpy as np

PatternSize = tuple[int, int]
BBox = tuple[float, float, float, float]
Point2 = tuple[float, float]

# EXHAUSTIVE trades runtime for recall on tilted/blurred boards; ACCURACY runs
# the subpixel refinement that makes a second cornerSubPix pass redundant.
SB_FLAGS = cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY


@dataclass(frozen=True)
class CheckerboardTarget:
    """The printed board: inner-corner counts and the measured square pitch."""

    pattern: PatternSize
    square_size: float

    @property
    def num_corners(self) -> int:
        return self.pattern[0] * self.pattern[1]

    def object_grid(self) -> np.ndarray:
        """Planar (z=0) object points, row-major to match the detector's order."""
        cols, rows = self.pattern
        grid = np.zeros((rows * cols, 3), np.float32)
        grid[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * self.square_size
        return grid


@dataclass(frozen=True)
class ViewDiagnostics:
    """Per-image capture quality, recorded whether or not detection succeeded."""

    detected: bool
    num_corners: int
    stamp_sec: float
    brightness: float
    sharpness: float
    bbox: BBox
    coverage_pct: float
    centroid_norm: Point2


@dataclass(frozen=True)
class CaptureDiagnostics:
    """One capture attempt, as written to the experiment metadata files."""

    index: int
    attempt: int
    accepted: bool
    reason: str
    split: str
    stamp_skew_sec: float
    left: ViewDiagnostics
    right: ViewDiagnostics
    corners_left: np.ndarray | None = field(default=None, repr=False)
    corners_right: np.ndarray | None = field(default=None, repr=False)

    def flat_record(self) -> dict[str, float | int | str | bool]:
        """One flat row per capture, shared by the JSON and CSV writers."""
        record: dict[str, float | int | str | bool] = {
            "index": self.index,
            "attempt": self.attempt,
            "accepted": self.accepted,
            "reason": self.reason,
            "split": self.split,
            "stamp_skew_sec": self.stamp_skew_sec,
        }
        for side, view in (("left", self.left), ("right", self.right)):
            record[f"{side}_detected"] = view.detected
            record[f"{side}_num_corners"] = view.num_corners
            record[f"{side}_stamp_sec"] = view.stamp_sec
            record[f"{side}_brightness"] = view.brightness
            record[f"{side}_sharpness"] = view.sharpness
            record[f"{side}_bbox_x0"], record[f"{side}_bbox_y0"] = view.bbox[0], view.bbox[1]
            record[f"{side}_bbox_x1"], record[f"{side}_bbox_y1"] = view.bbox[2], view.bbox[3]
            record[f"{side}_coverage_pct"] = view.coverage_pct
            record[f"{side}_centroid_x_norm"] = view.centroid_norm[0]
            record[f"{side}_centroid_y_norm"] = view.centroid_norm[1]
        return record


def sharpness(gray: np.ndarray) -> float:
    """Variance of the Laplacian — low values flag motion blur or defocus."""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def detect(gray: np.ndarray, pattern: PatternSize) -> np.ndarray | None:
    """Locate the complete inner-corner grid, or None if it is not fully visible.

    findChessboardCornersSB already returns subpixel coordinates, so no
    cornerSubPix pass follows it.
    """
    found, corners = cv2.findChessboardCornersSB(gray, pattern, flags=SB_FLAGS)
    if not found or corners is None or len(corners) != pattern[0] * pattern[1]:
        return None
    # The build returns either (N,2) or (N,1,2); downstream OpenCV calls all
    # want the channelled form.
    return _canonical(np.asarray(corners, dtype=np.float32).reshape(-1, 1, 2))


def _canonical(corners: np.ndarray) -> np.ndarray:
    """Resolve the detector's 180-degree origin ambiguity.

    A rectangular grid comes back row-major from one of two opposite origins.
    Left and right must pick the same one or every correspondence is reversed;
    anchoring on the top-left-most endpoint agrees across a short baseline.
    """
    first, last = corners[0, 0], corners[-1, 0]
    if (first[0] + first[1]) > (last[0] + last[1]):
        return corners[::-1].copy()
    return corners


def measure(gray: np.ndarray, corners: np.ndarray | None, stamp_sec: float) -> ViewDiagnostics:
    """Build the capture-quality record for one image of a stereo pair."""
    height, width = gray.shape[:2]
    if corners is None:
        return ViewDiagnostics(
            detected=False,
            num_corners=0,
            stamp_sec=stamp_sec,
            brightness=float(np.mean(gray)),
            sharpness=sharpness(gray),
            bbox=(0.0, 0.0, 0.0, 0.0),
            coverage_pct=0.0,
            centroid_norm=(0.0, 0.0),
        )

    pts = corners.reshape(-1, 2)
    x0, y0 = float(pts[:, 0].min()), float(pts[:, 1].min())
    x1, y1 = float(pts[:, 0].max()), float(pts[:, 1].max())
    return ViewDiagnostics(
        detected=True,
        num_corners=int(len(pts)),
        stamp_sec=stamp_sec,
        brightness=float(np.mean(gray)),
        sharpness=sharpness(gray),
        bbox=(x0, y0, x1, y1),
        coverage_pct=100.0 * (x1 - x0) * (y1 - y0) / float(width * height),
        centroid_norm=(float(pts[:, 0].mean()) / width, float(pts[:, 1].mean()) / height),
    )
