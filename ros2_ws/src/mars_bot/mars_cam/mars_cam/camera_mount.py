# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Per-robot camera mount correction: how far the real camera sits from the URDF.

The URDF places the camera from nominal CAD, and the head servo zero is a single
hardcoded homing_offset shared across every robot, so per-unit assembly variation
lands in TF uncorrected. Left alone it tilts the reconstructed floor, which the
costmap marks as obstacles.

Stored beside ``stereo_calib.yaml`` in the per-robot calibration directory and in
the same OpenCV FileStorage format, so the C++ side reads it with the same
``cv::FileStorage`` it already uses and the two never drift apart.
"""

import time
from dataclasses import dataclass
from pathlib import Path

import cv2

MOUNT_FILE_NAME = "camera_mount.yaml"
MOUNT_FILE_VERSION = 1


@dataclass(frozen=True)
class MountCorrection:
    """Absolute correction applied to the depth cloud, not a delta.

    ``head_angle_deg`` is recorded because the floor tilt drifts with head angle
    (~0.027 deg per degree on R7-27), so a correction measured at one angle is
    only exact at that angle.
    """

    pitch_deg: float = 0.0
    roll_deg: float = 0.0
    height_m: float = 0.0
    head_angle_deg: float = 0.0
    residual_rms_mm: float = 0.0
    measured_at: str = ""

    def plus(self, pitch_delta: float, roll_delta: float, height_delta: float) -> "MountCorrection":
        """Fold in a freshly measured residual, keeping the values absolute."""
        return MountCorrection(
            pitch_deg=self.pitch_deg + pitch_delta,
            roll_deg=self.roll_deg + roll_delta,
            height_m=self.height_m + height_delta,
            head_angle_deg=self.head_angle_deg,
            residual_rms_mm=self.residual_rms_mm,
            measured_at=self.measured_at,
        )


def mount_path(calibration_dir: Path) -> Path:
    return calibration_dir / MOUNT_FILE_NAME


def load(path: Path) -> MountCorrection | None:
    """Read a mount file, or None if it is absent or unreadable.

    A missing file is normal — an uncalibrated robot runs with zero correction
    rather than refusing to start.
    """
    if not path.exists():
        return None
    storage = cv2.FileStorage(str(path), cv2.FileStorage_READ)
    if not storage.isOpened():
        return None
    try:
        version = int(storage.getNode("version").real())
        if version != MOUNT_FILE_VERSION:
            return None
        return MountCorrection(
            pitch_deg=storage.getNode("pitch_deg").real(),
            roll_deg=storage.getNode("roll_deg").real(),
            height_m=storage.getNode("height_m").real(),
            head_angle_deg=storage.getNode("head_angle_deg").real(),
            residual_rms_mm=storage.getNode("residual_rms_mm").real(),
            measured_at=storage.getNode("measured_at").string() or "",
        )
    finally:
        storage.release()


def save(path: Path, correction: MountCorrection) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    storage = cv2.FileStorage(str(path), cv2.FileStorage_WRITE)
    if not storage.isOpened():
        raise RuntimeError(f"Failed to open mount file for writing: {path}")
    try:
        storage.write("version", MOUNT_FILE_VERSION)
        storage.write("pitch_deg", correction.pitch_deg)
        storage.write("roll_deg", correction.roll_deg)
        storage.write("height_m", correction.height_m)
        storage.write("head_angle_deg", correction.head_angle_deg)
        storage.write("residual_rms_mm", correction.residual_rms_mm)
        storage.write("measured_at", correction.measured_at or time.strftime("%Y-%m-%dT%H:%M:%S"))
    finally:
        storage.release()
    return path


def find_calibration_dir(data_directory: Path) -> Path | None:
    """The per-robot calibration directory, matching the C++ loader's rule."""
    if not data_directory.exists():
        return None
    for entry in sorted(data_directory.iterdir()):
        if entry.is_dir() and "calibration_config" in entry.name:
            return entry
    return None
