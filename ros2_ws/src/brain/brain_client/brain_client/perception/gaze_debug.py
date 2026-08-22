# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""JSON wire shape for the Agent camera's gaze overlay."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypedDict

from brain_client.common.enums import StrEnum
from brain_client.perception.face_lock import CENTER_ZONE, FaceBox


class GazeStatus(StrEnum):
    OFF = "off"
    PAUSED = "paused"
    STARTING = "starting"
    SEARCHING = "searching"
    FOLLOWING = "following"
    TOO_FAR = "too_far"
    CENTERING = "centering"
    LOCKED = "locked"


class DebugBox(TypedDict):
    center_x: float
    center_y: float
    width: float
    height: float


class DebugZone(TypedDict):
    left: float
    top: float
    right: float
    bottom: float


class DebugImage(TypedDict):
    width: int
    height: int


class GazeDebug(TypedDict):
    status: GazeStatus
    progress: float
    faces: list[DebugBox]
    target: DebugBox | None
    zone: DebugZone
    image: DebugImage
    frame: int


def gaze_debug(
    status: GazeStatus,
    *,
    faces: Sequence[FaceBox] = (),
    target: FaceBox | None = None,
    progress: float = 0.0,
    image_width: int = 0,
    image_height: int = 0,
    frame: int = 0,
) -> GazeDebug:
    return {
        "status": status,
        "progress": progress,
        "faces": [_box(face) for face in faces],
        "target": _box(target) if target is not None else None,
        "zone": {
            "left": CENTER_ZONE.left,
            "top": CENTER_ZONE.top,
            "right": CENTER_ZONE.right,
            "bottom": CENTER_ZONE.bottom,
        },
        "image": {"width": image_width, "height": image_height},
        "frame": frame,
    }


def _box(face: FaceBox) -> DebugBox:
    return {
        "center_x": face.center_x,
        "center_y": face.center_y,
        "width": face.width,
        "height": face.height,
    }
