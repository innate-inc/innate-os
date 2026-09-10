# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""On-disk artefacts for a checkerboard calibration experiment.

Everything a run produces — capture metadata, the candidate calibration, the
held-out validation report and the debug renders — lands under one timestamped
directory that is never the production calibration directory.
"""

import csv
import json
import time
from pathlib import Path

import cv2
import numpy as np

from mars_cam.calibration_validation import ErrorStats, ValidationReport
from mars_cam.checkerboard import CaptureDiagnostics

# The C++ loader claims any directory whose name contains this substring, so an
# experiment directory must never include it or it can shadow production.
_PRODUCTION_DIR_MARKER = "calibration_config"

CANDIDATE_CALIB_NAME = "stereo_calib_candidate.yaml"


class ExperimentRecorder:
    """Owns one experiment run's output directory and writes its artefacts."""

    def __init__(self, root: Path, target_type: str) -> None:
        if _PRODUCTION_DIR_MARKER in root.name:
            raise ValueError(f"Experiment root must not contain '{_PRODUCTION_DIR_MARKER}': {root}")
        self.run_dir = root / f"{target_type}_{time.strftime('%Y%m%d_%H%M%S')}"
        self.image_dir = self.run_dir / "images"
        self.debug_dir = self.run_dir / "debug"
        for directory in (self.image_dir, self.debug_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- images

    def save_pair(self, index: int, left: np.ndarray, right: np.ndarray) -> None:
        cv2.imwrite(str(self.image_dir / f"left_{index:03d}.png"), left)
        cv2.imwrite(str(self.image_dir / f"right_{index:03d}.png"), right)

    def save_detection_overlay(
        self,
        index: int,
        left: np.ndarray,
        right: np.ndarray,
        corners_left: np.ndarray,
        corners_right: np.ndarray,
        pattern: tuple[int, int],
    ) -> None:
        panels = []
        for image, corners in ((left, corners_left), (right, corners_right)):
            panel = image.copy()
            cv2.drawChessboardCorners(panel, pattern, corners, True)
            panels.append(panel)
        cv2.imwrite(str(self.debug_dir / f"detection_{index:03d}.png"), np.hstack(panels))

    def save_coverage(self, corners_left: list[np.ndarray], corners_right: list[np.ndarray], size: tuple[int, int]):
        """Dot map of every accepted corner, per camera, as one side-by-side image."""
        width, height = size
        panels = []
        for corner_sets, color in ((corners_left, (80, 80, 255)), (corners_right, (80, 255, 80))):
            canvas = np.zeros((height, width, 3), dtype=np.uint8)
            for corners in corner_sets:
                for point in corners.reshape(-1, 2):
                    x, y = int(point[0]), int(point[1])
                    if 0 <= x < width and 0 <= y < height:
                        cv2.circle(canvas, (x, y), 2, color, -1)
            panels.append(canvas)
        cv2.imwrite(str(self.debug_dir / "coverage.png"), np.hstack(panels))

    def save_rectified_samples(
        self,
        pairs: list[tuple[int, np.ndarray, np.ndarray]],
        calib: dict,
        size: tuple[int, int],
        line_spacing: int = 32,
    ) -> None:
        """Rectified left/right pairs with horizontal guides for eyeballing alignment."""
        width, height = size
        map_left = cv2.initUndistortRectifyMap(
            calib["K1"], calib["D1"], calib["R1"], calib["P1"], (width, height), cv2.CV_32FC1
        )
        map_right = cv2.initUndistortRectifyMap(
            calib["K2"], calib["D2"], calib["R2"], calib["P2"], (width, height), cv2.CV_32FC1
        )
        for index, left, right in pairs:
            rect_left = cv2.remap(left, map_left[0], map_left[1], cv2.INTER_LINEAR)
            rect_right = cv2.remap(right, map_right[0], map_right[1], cv2.INTER_LINEAR)
            canvas = np.hstack([rect_left, rect_right])
            for y in range(0, height, line_spacing):
                cv2.line(canvas, (0, y), (canvas.shape[1], y), (0, 255, 255), 1)
            cv2.imwrite(str(self.debug_dir / f"rectified_{index:03d}.png"), canvas)

    # -------------------------------------------------------------- metadata

    def write_captures(self, diagnostics: list[CaptureDiagnostics]) -> tuple[Path, Path]:
        rows = [d.flat_record() for d in diagnostics]
        json_path = self.run_dir / "captures.json"
        csv_path = self.run_dir / "captures.csv"
        json_path.write_text(json.dumps(rows, indent=2))
        _write_csv(csv_path, rows)
        return json_path, csv_path

    def write_validation(self, report: ValidationReport) -> tuple[Path, Path]:
        payload = report.to_dict()
        corner_rows = payload.pop("corner_samples", [])
        json_path = self.run_dir / "validation_report.json"
        json_path.write_text(json.dumps(payload, indent=2))
        _write_csv(self.run_dir / "corner_errors.csv", corner_rows)
        markdown_path = self.run_dir / "validation_report.md"
        markdown_path.write_text(render_markdown(report))
        return json_path, markdown_path

    def write_summary(self, summary: dict) -> Path:
        path = self.run_dir / "run_summary.json"
        path.write_text(json.dumps(summary, indent=2, default=str))
        return path

    def save_candidate_calibration(self, calib: dict) -> Path:
        """Write the solved calibration as a candidate, never as production."""
        path = self.run_dir / CANDIDATE_CALIB_NAME
        storage = cv2.FileStorage(str(path), cv2.FileStorage_WRITE)
        if not storage.isOpened():
            raise RuntimeError(f"Failed to open candidate calibration for writing: {path}")
        storage.write("version", 2)
        storage.write("model", "pinhole")
        storage.write("image_width", calib["image_width"])
        storage.write("image_height", calib["image_height"])
        for key in ("K1", "D1", "K2", "D2", "R", "T", "R1", "R2", "P1", "P2", "Q"):
            storage.write(key, calib[key])
        storage.release()
        return path


def skew_summary(diagnostics: list[CaptureDiagnostics]) -> dict[str, float | int]:
    """Observed left/right timestamp skew, reported before any tolerance change."""
    skews = np.array([abs(d.stamp_skew_sec) for d in diagnostics if d.left.stamp_sec > 0.0])
    if skews.size == 0:
        return {"count": 0}
    return {
        "count": int(skews.size),
        "mean_ms": float(np.mean(skews) * 1000.0),
        "median_ms": float(np.median(skews) * 1000.0),
        "p95_ms": float(np.percentile(skews, 95) * 1000.0),
        "max_ms": float(np.max(skews) * 1000.0),
        "std_ms": float(np.std(skews) * 1000.0),
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _stats_row(label: str, stats: ErrorStats, unit: str) -> str:
    return (
        f"| {label} ({unit}) | {stats.count} | {stats.mean:.4f} | {stats.rms:.4f} | "
        f"{stats.median:.4f} | {stats.p95:.4f} | {stats.maximum:.4f} |"
    )


def render_markdown(report: ValidationReport) -> str:
    """Human-readable validation summary written beside the machine-readable JSON."""
    lines = [
        "# Held-out stereo calibration validation",
        "",
        f"Validation pairs (never seen by calibrateCamera / stereoCalibrate): **{report.num_pairs}**",
        "",
        "## Overall",
        "",
        "| Metric | N | Mean | RMS | Median | p95 | Max |",
        "|---|---|---|---|---|---|---|",
        _stats_row("Left reprojection", report.left_reprojection, "px"),
        _stats_row("Right reprojection", report.right_reprojection, "px"),
        _stats_row("Rectified epipolar abs(dy)", report.epipolar_dy, "px"),
        _stats_row("Neighbour spacing error", report.spacing_error_mm, "mm"),
        _stats_row("Planarity RMS per image", report.planarity_rms_mm, "mm"),
        _stats_row("Planarity p95 per image", report.planarity_p95_mm, "mm"),
        "",
        f"Mean reconstructed neighbour spacing: **{report.mean_spacing_mm:.3f} mm** "
        f"(nominal {report.nominal_spacing_mm:.3f} mm)",
        "",
        f"Median triangulated depth: **{report.median_depth_m:+.4f} m** — "
        f"Q yields positive Z for points in front of the camera: **{report.q_yields_positive_z}**",
        "",
        "## Error versus image location",
        "",
        "| Ring | r range | N | Reproj mean (px) | Reproj p95 (px) | abs(dy) mean (px) | abs(dy) p95 (px) |",
        "|---|---|---|---|---|---|---|",
    ]
    for ring in report.radial_bins:
        upper = "1.00+" if ring.r_max == float("inf") else f"{ring.r_max:.2f}"
        lines.append(
            f"| {ring.label} | {ring.r_min:.2f}–{upper} | {ring.reprojection.count} | "
            f"{ring.reprojection.mean:.4f} | {ring.reprojection.p95:.4f} | "
            f"{ring.epipolar_dy.mean:.4f} | {ring.epipolar_dy.p95:.4f} |"
        )

    lines += [
        "",
        "## Per validation image",
        "",
        "| Image | L reproj RMS (px) | R reproj RMS (px) | abs(dy) RMS (px) | Spacing err mean (mm) | "
        "Planarity RMS (mm) | Median depth (m) |",
        "|---|---|---|---|---|---|---|",
    ]
    for image in report.per_image:
        lines.append(
            f"| {image.index} | {image.left_reprojection.rms:.4f} | {image.right_reprojection.rms:.4f} | "
            f"{image.epipolar_dy.rms:.4f} | {image.spacing_error_mm.mean:.4f} | "
            f"{image.planarity_rms_mm:.4f} | {image.median_depth_m:+.4f} |"
        )
    lines.append("")
    return "\n".join(lines)
