# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""JSON wire shape for the Agent camera's gaze overlay."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TypedDict

from brain_client.common.enums import StrEnum
from brain_client.perception.face_lock import CENTER_ZONE, FaceBox
from brain_client.perception.yolo_pose import (
    MODEL_INPUT_SIZE,
    MODEL_NAME,
    MODEL_RUNTIME,
    PersonPose,
)


class GazeStatus(StrEnum):
    OFF = "off"
    PAUSED = "paused"
    STARTING = "starting"
    ERROR = "error"
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


class DebugKeypoint(TypedDict):
    name: str
    x: float
    y: float
    confidence: float


class DebugPerson(TypedDict):
    confidence: float
    target: bool
    head_visible: bool
    body: DebugBox
    head: DebugBox
    keypoints: list[DebugKeypoint]


class DebugDetector(TypedDict):
    model: str
    runtime: str
    input_size: int
    inference_ms: float
    error: str


class DebugFollowGoal(TypedDict):
    x: float
    y: float
    yaw_degrees: float


class DebugFollow(TypedDict):
    enabled: bool
    state: str
    reference_height: float
    observed_height: float
    body_center_x: float | None
    forward_m: float
    bearing_degrees: float
    perception_age_ms: float
    target_age_ms: float
    reacquiring: bool
    nav_state: str
    nav_pending: int
    nav_active: int
    nav_canceling: int
    goal: DebugFollowGoal | None
    stop_reason: str
    nav_reason: str


class GazeDebug(TypedDict):
    status: GazeStatus
    progress: float
    detector: DebugDetector
    faces: list[DebugBox]
    target: DebugBox | None
    people: list[DebugPerson]
    zone: DebugZone
    image: DebugImage
    frame: int
    follow: DebugFollow


def gaze_debug(
    status: GazeStatus,
    *,
    faces: Sequence[FaceBox] = (),
    target: FaceBox | None = None,
    people: Sequence[PersonPose] = (),
    progress: float = 0.0,
    inference_ms: float = 0.0,
    error: str = "",
    image_width: int = 0,
    image_height: int = 0,
    frame: int = 0,
    follow: DebugFollow | None = None,
) -> GazeDebug:
    return {
        "status": status,
        "progress": progress,
        "detector": {
            "model": MODEL_NAME,
            "runtime": MODEL_RUNTIME,
            "input_size": MODEL_INPUT_SIZE,
            "inference_ms": round(inference_ms, 1),
            "error": error,
        },
        "faces": [_box(face) for face in faces],
        "target": _box(target) if target is not None else None,
        "people": [_person(person, target) for person in people],
        "zone": {
            "left": CENTER_ZONE.left,
            "top": CENTER_ZONE.top,
            "right": CENTER_ZONE.right,
            "bottom": CENTER_ZONE.bottom,
        },
        "image": {"width": image_width, "height": image_height},
        "frame": frame,
        "follow": follow
        or {
            "enabled": False,
            "state": "idle",
            "reference_height": 0.0,
            "observed_height": 0.0,
            "body_center_x": None,
            "forward_m": 0.0,
            "bearing_degrees": 0.0,
            "perception_age_ms": 0.0,
            "target_age_ms": 0.0,
            "reacquiring": False,
            "nav_state": "idle",
            "nav_pending": 0,
            "nav_active": 0,
            "nav_canceling": 0,
            "goal": None,
            "stop_reason": "",
            "nav_reason": "",
        },
    }


def _box(face: FaceBox) -> DebugBox:
    return {
        "center_x": face.center_x,
        "center_y": face.center_y,
        "width": face.width,
        "height": face.height,
    }


def _person(person: PersonPose, target: FaceBox | None) -> DebugPerson:
    return {
        "confidence": round(person.confidence, 3),
        "target": person.head is target,
        "head_visible": person.head.lockable,
        "body": _box(person.body),
        "head": _box(person.head),
        "keypoints": [
            {
                "name": str(point.name),
                "x": point.x,
                "y": point.y,
                "confidence": round(point.confidence, 3),
            }
            for point in person.keypoints
            if point.visible
        ],
    }
