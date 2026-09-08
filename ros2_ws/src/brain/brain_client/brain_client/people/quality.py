# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Which frames are allowed to carry identity evidence — RFC 4.5 as code.

At 15 fps with hardware auto-exposure indoors, motion blur is the normal case:
a person walking 1 m/s two metres away smears a 32 px face by ~4 px, and the
robot turning adds more. The engine does not fight blur, it harvests the still
instants — so every gate here is a veto, and what survives carries a weight
(:func:`quality_score`) rather than a yes.

Face sizes are always NATIVE pixels (the 1280x720 left eye); a measurement taken
from the published 640x480 frame is converted before it gets here, or the gates
would silently move with the frame path.

PURE module: numpy and cv2 for the pixel measures, no ROS, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from brain_client.common.enums import StrEnum
from brain_client.people.geometry import box_center, box_size
from brain_client.people.types import Box

# ---------------------------------------------------------------- thresholds

FACE_MIN_DETECT_PX = 24.0  # enough to say "a face, roughly there" (~3.5 m)
FACE_MIN_MATCH_PX = 40.0  # ~2.5 m, the 10 px interpupillary floor for ArcFace-class models
FACE_MIN_ENROL_PX = 48.0
FACE_MAX_YAW_MATCH_DEG = 45.0
FACE_MAX_YAW_ENROL_DEG = 30.0
# Pitch is signed positive when the face is seen from below, which is the pose
# ArcFace handles worst and the pose a knee-high robot gets by default.
FACE_MAX_PITCH_MATCH_DEG = 40.0
FACE_MAX_PITCH_ENROL_DEG = 25.0

BODY_MIN_MATCH_PX = 96.0
BODY_MIN_OUTFIT_PX = 128.0  # a 256x128 ReID input, which holds to ~5.3 m
BODY_MIN_SCORE = 0.5

LUMINANCE_MIN, LUMINANCE_MAX = 40.0, 220.0

# Variance of the Laplacian, measured at _ANALYSIS_PX so the crop's own
# resolution cannot move the threshold. A small face upscaled to the analysis
# size carries less high-frequency detail even when perfectly sharp, so the
# floor scales down with it.
_ANALYSIS_PX = 112  # SFace's own input size
FACE_SHARPNESS_FLOOR = 12.0
BODY_SHARPNESS_FLOOR = 8.0

FACE_INDEPENDENCE_SEC = 0.15
BODY_INDEPENDENCE_SEC = 0.5
INDEPENDENCE_BOX_MOVE = 0.05  # centre travel as a fraction of the box's own size

# Ego-motion: the robot's own movement smears every pixel, so no identity
# decision is made inside these windows (perception/camera.py's own rule).
DRIVE_SUPPRESS_SEC = 1.5
HEAD_PITCH_EPS_DEG = 0.8
YAW_RATE_MAX = 0.2  # rad/s

_YAW_BUCKET_DEG = 20.0
_PITCH_BUCKET_DEG = 25.0


class Purpose(StrEnum):
    """What a frame is being judged for; enrolment is stricter than matching."""

    MATCH = "match"
    ENROL = "enrol"


# ------------------------------------------------------------- pixel measures


def _analysis_gray(crop_bgr: np.ndarray) -> np.ndarray | None:
    if crop_bgr.size == 0:
        return None
    gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY) if crop_bgr.ndim == 3 else crop_bgr
    longest = max(gray.shape[0], gray.shape[1])
    if longest != _ANALYSIS_PX:
        scale = _ANALYSIS_PX / longest
        size = (max(1, round(gray.shape[1] * scale)), max(1, round(gray.shape[0] * scale)))
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        gray = cv2.resize(gray, size, interpolation=interpolation)
    return gray


def crop_sharpness(crop_bgr: np.ndarray) -> float:
    """Variance of the Laplacian at the analysis scale (memory/quality.py's measure)."""
    gray = _analysis_gray(crop_bgr)
    if gray is None:
        return 0.0
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def crop_luminance(crop_bgr: np.ndarray) -> float:
    gray = _analysis_gray(crop_bgr)
    if gray is None:
        return 0.0
    return float(gray.mean())


# -------------------------------------------------------------------- gates


def face_detectable(size_px: float) -> bool:
    return size_px >= FACE_MIN_DETECT_PX


def face_size_ok(size_px: float, purpose: Purpose = Purpose.MATCH) -> bool:
    return size_px >= (FACE_MIN_ENROL_PX if purpose is Purpose.ENROL else FACE_MIN_MATCH_PX)


def face_pose_ok(yaw_deg: float, pitch_deg: float, purpose: Purpose = Purpose.MATCH) -> bool:
    enrol = purpose is Purpose.ENROL
    max_yaw = FACE_MAX_YAW_ENROL_DEG if enrol else FACE_MAX_YAW_MATCH_DEG
    max_pitch = FACE_MAX_PITCH_ENROL_DEG if enrol else FACE_MAX_PITCH_MATCH_DEG
    return abs(yaw_deg) <= max_yaw and abs(pitch_deg) <= max_pitch


def min_face_sharpness(size_px: float) -> float:
    return FACE_SHARPNESS_FLOOR * min(1.0, max(size_px, 0.0) / FACE_MIN_ENROL_PX)


def face_sharp_enough(sharpness: float, size_px: float) -> bool:
    return sharpness >= min_face_sharpness(size_px)


def luminance_ok(luminance: float) -> bool:
    return LUMINANCE_MIN <= luminance <= LUMINANCE_MAX


def body_size_ok(height_px: float, purpose: Purpose = Purpose.MATCH) -> bool:
    return height_px >= (BODY_MIN_OUTFIT_PX if purpose is Purpose.ENROL else BODY_MIN_MATCH_PX)


def body_score_ok(score: float) -> bool:
    return score >= BODY_MIN_SCORE


def body_sharp_enough(sharpness: float) -> bool:
    return sharpness >= BODY_SHARPNESS_FLOOR


def pose_bucket(yaw_deg: float, pitch_deg: float) -> str:
    """Coarse pose label: the diversity axis for templates and for independence."""
    if pitch_deg > _PITCH_BUCKET_DEG:
        return "up"  # the face is seen from below, i.e. tilted up away from the lens
    if pitch_deg < -_PITCH_BUCKET_DEG:
        return "down"
    if yaw_deg > _YAW_BUCKET_DEG:
        return "right"
    if yaw_deg < -_YAW_BUCKET_DEG:
        return "left"
    return "frontal"


def box_movement(a: Box, b: Box) -> float:
    """Centre travel between two boxes as a fraction of their own size, so a
    person at 4 m does not have to travel as far as one at 1 m to show the
    detector something new."""
    ax, ay = box_center(a)
    bx, by = box_center(b)
    aw, ah = box_size(a)
    bw, bh = box_size(b)
    scale = (float(np.hypot(aw, ah)) + float(np.hypot(bw, bh))) / 2.0
    if scale < 1e-6:
        return 0.0
    return float(np.hypot(bx - ax, by - ay)) / scale


def independent(
    stamp: float,
    box: Box,
    bucket: str,
    *,
    last_stamp: float,
    last_box: Box,
    last_bucket: str,
    min_gap_sec: float = FACE_INDEPENDENCE_SEC,
) -> bool:
    """RFC 4.5: a frame counts separately only when it is far enough apart in
    time AND shows something new — a different pose bucket or a moved box.
    Adjacent near-duplicates of one instant are one piece of evidence."""
    if stamp - last_stamp < min_gap_sec:
        return False
    return bucket != last_bucket or box_movement(last_box, box) >= INDEPENDENCE_BOX_MOVE


class IndependenceGate:
    """Per-key memory of the last frame that was allowed to count."""

    def __init__(self, min_gap_sec: float = FACE_INDEPENDENCE_SEC) -> None:
        self._min_gap = min_gap_sec
        self._last: dict[str, tuple[float, Box, str]] = {}

    def accept(self, key: str, stamp: float, box: Box, bucket: str) -> bool:
        last = self._last.get(key)
        if last is not None and not independent(
            stamp, box, bucket, last_stamp=last[0], last_box=last[1], last_bucket=last[2], min_gap_sec=self._min_gap
        ):
            return False
        self._last[key] = (stamp, box, bucket)
        return True

    def forget(self, key: str) -> None:
        self._last.pop(key, None)


# ------------------------------------------------------------ quality score


def _span(value: float, low: float, high: float) -> float:
    if high <= low:
        return 1.0
    return min(1.0, max(0.0, (value - low) / (high - low)))


def quality_score(
    *,
    size_px: float,
    yaw_deg: float,
    pitch_deg: float,
    sharpness: float,
    luminance: float,
    fiqa: float | None = None,
) -> float:
    """The frame's evidence weight in 0-1: geometry and focus, scaled by
    eDifFIQA(T) when a quality model is loaded (RFC 4.4)."""
    size = 0.4 + 0.6 * _span(size_px, FACE_MIN_DETECT_PX, 2 * FACE_MIN_ENROL_PX)
    pose = max(0.0, 1.0 - abs(yaw_deg) / 90.0) * max(0.0, 1.0 - abs(pitch_deg) / 90.0)
    focus = _span(sharpness, min_face_sharpness(size_px), 4.0 * FACE_SHARPNESS_FLOOR)
    mid = (LUMINANCE_MIN + LUMINANCE_MAX) / 2.0
    exposure = max(0.0, 1.0 - abs(luminance - mid) / (mid - LUMINANCE_MIN))
    score = size * pose * (0.3 + 0.7 * focus) * (0.5 + 0.5 * exposure)
    if fiqa is not None:
        score *= min(1.0, max(0.0, fiqa))
    return min(1.0, max(0.0, score))


def body_quality_score(*, height_px: float, score: float, sharpness: float) -> float:
    size = _span(height_px, BODY_MIN_MATCH_PX, 2 * BODY_MIN_OUTFIT_PX)
    focus = _span(sharpness, BODY_SHARPNESS_FLOOR, 4.0 * BODY_SHARPNESS_FLOOR)
    return min(1.0, max(0.0, min(1.0, score) * (0.4 + 0.6 * size) * (0.4 + 0.6 * focus)))


# -------------------------------------------------------------- ego-motion


@dataclass(frozen=True)
class EgoMotion:
    """The robot's own movement at one instant. ``still`` is the precondition
    for every identity decision and for decoding a native frame."""

    stamp: float = 0.0
    last_drive: float = -1e9  # epoch seconds of the last nonzero cmd_vel
    head_pitch_deg: float = 0.0
    head_pitch_delta_deg: float = 0.0
    yaw_rate: float = 0.0

    @property
    def recently_driven(self) -> bool:
        return self.stamp - self.last_drive < DRIVE_SUPPRESS_SEC

    @property
    def head_moving(self) -> bool:
        return abs(self.head_pitch_delta_deg) >= HEAD_PITCH_EPS_DEG

    @property
    def turning(self) -> bool:
        return abs(self.yaw_rate) >= YAW_RATE_MAX

    @property
    def still(self) -> bool:
        return not (self.recently_driven or self.head_moving or self.turning)

    @classmethod
    def still_at(cls, stamp: float, head_pitch_deg: float = 0.0) -> EgoMotion:
        return cls(stamp=stamp, head_pitch_deg=head_pitch_deg)


@dataclass
class EgoMotionTracker:
    """Folds cmd_vel, head pitch and odometry yaw rate into an :class:`EgoMotion`.

    Pure: the node feeds it from its callbacks. Stamps are epoch seconds, as
    everywhere in this package.
    """

    last_drive: float = field(default=-1e9)
    head_pitch_deg: float = 0.0
    yaw_rate: float = 0.0
    _pitch_at: float = field(default=0.0, repr=False)
    _pitch_delta: float = field(default=0.0, repr=False)

    def note_cmd_vel(self, stamp: float, linear_x: float, linear_y: float, angular_z: float) -> None:
        if any((linear_x, linear_y, angular_z)):
            self.last_drive = stamp

    def note_head_pitch(self, stamp: float, pitch_deg: float) -> None:
        self._pitch_delta = pitch_deg - self.head_pitch_deg
        self.head_pitch_deg = pitch_deg
        self._pitch_at = stamp

    def note_yaw_rate(self, stamp: float, yaw_rate: float) -> None:
        self.yaw_rate = yaw_rate
        del stamp  # rate is instantaneous; only the latest reading matters

    def state(self, now: float) -> EgoMotion:
        # A pitch step older than the drive window is over; keep the last known
        # pitch but stop reporting the head as moving.
        delta = self._pitch_delta if now - self._pitch_at < DRIVE_SUPPRESS_SEC else 0.0
        return EgoMotion(
            stamp=now,
            last_drive=self.last_drive,
            head_pitch_deg=self.head_pitch_deg,
            head_pitch_delta_deg=delta,
            yaw_rate=self.yaw_rate,
        )
