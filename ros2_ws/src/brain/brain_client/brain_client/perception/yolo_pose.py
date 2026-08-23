# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""YOLOv8n-Pose inference and head-target extraction.

The vendored 320 px ONNX graph was exported from Ultralytics 8.4.126 with
opset 17 and simplify enabled (sha256
0ed2199bdbdf45ce574e638373499b8d4646b14610bdacfdae674b3615858647).
The weights require separate Ultralytics licensing approval before distribution.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

import cv2
import numpy as np
import numpy.typing as npt

from brain_client.common.enums import StrEnum
from brain_client.perception.face_lock import FaceBox

MODEL_PATH = Path(__file__).with_name("yolov8n-pose.onnx")
MODEL_SHA256 = "0ed2199bdbdf45ce574e638373499b8d4646b14610bdacfdae674b3615858647"
MODEL_INPUT_SIZE = 320
MODEL_NAME = "yolov8n-pose"
MODEL_RUNTIME = "onnxruntime-cpu"

_DETECTION_CONFIDENCE = 0.35
_KEYPOINT_CONFIDENCE = 0.40
_NMS_IOU = 0.45
_MAX_PEOPLE = 3
_BODY_FALLBACK_MIN_WIDTH = 0.30


class KeypointName(StrEnum):
    NOSE = "nose"
    LEFT_EYE = "left_eye"
    RIGHT_EYE = "right_eye"
    LEFT_EAR = "left_ear"
    RIGHT_EAR = "right_ear"
    LEFT_SHOULDER = "left_shoulder"
    RIGHT_SHOULDER = "right_shoulder"
    LEFT_ELBOW = "left_elbow"
    RIGHT_ELBOW = "right_elbow"
    LEFT_WRIST = "left_wrist"
    RIGHT_WRIST = "right_wrist"
    LEFT_HIP = "left_hip"
    RIGHT_HIP = "right_hip"
    LEFT_KNEE = "left_knee"
    RIGHT_KNEE = "right_knee"
    LEFT_ANKLE = "left_ankle"
    RIGHT_ANKLE = "right_ankle"


_KEYPOINT_NAMES = tuple(KeypointName)
_FACE_NAMES = frozenset(
    {
        KeypointName.NOSE,
        KeypointName.LEFT_EYE,
        KeypointName.RIGHT_EYE,
        KeypointName.LEFT_EAR,
        KeypointName.RIGHT_EAR,
    }
)


@dataclass(frozen=True)
class Keypoint:
    name: KeypointName
    x: float
    y: float
    confidence: float

    @property
    def visible(self) -> bool:
        return self.confidence >= _KEYPOINT_CONFIDENCE


@dataclass(frozen=True)
class PersonPose:
    body: FaceBox
    head: FaceBox
    confidence: float
    keypoints: tuple[Keypoint, ...]


@dataclass(frozen=True)
class DetectionFrame:
    people: tuple[PersonPose, ...]
    inference_ms: float


class _InferenceSession(Protocol):
    def run(
        self,
        output_names: None,
        input_feed: dict[str, npt.NDArray[np.float32]],
    ) -> list[npt.NDArray[np.float32]]: ...


class YoloPoseDetector:
    """One fixed-shape ONNX session mapping a BGR frame to visible head targets."""

    def __init__(self, model_path: Path = MODEL_PATH) -> None:
        try:
            import onnxruntime  # deferred: gaze-disabled agents must not load the runtime
        except ModuleNotFoundError as error:
            if error.name == "onnxruntime":
                raise RuntimeError("onnxruntime is not installed") from error
            raise

        if not model_path.is_file():
            raise FileNotFoundError(f"pose model not found: {model_path}")
        digest = hashlib.sha256()
        with model_path.open("rb") as model_file:
            for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != MODEL_SHA256:
            raise ValueError(f"pose model checksum mismatch: {model_path}")
        options = onnxruntime.SessionOptions()
        options.inter_op_num_threads = 1
        options.intra_op_num_threads = 1
        session = onnxruntime.InferenceSession(
            str(model_path),
            sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        input_meta = session.get_inputs()
        output_meta = session.get_outputs()
        if len(input_meta) != 1 or input_meta[0].name != "images" or input_meta[0].shape != [1, 3, 320, 320]:
            raise ValueError(f"unexpected pose model input: {[(item.name, item.shape) for item in input_meta]}")
        if len(output_meta) != 1 or output_meta[0].shape != [1, 56, 2100]:
            raise ValueError(f"unexpected pose model output: {[(item.name, item.shape) for item in output_meta]}")
        self._session = cast(_InferenceSession, cast(object, session))

    def detect(self, frame: npt.NDArray[np.uint8]) -> DetectionFrame:
        tensor, scale, pad_x, pad_y = _letterbox(frame)
        started = time.perf_counter()
        output = self._session.run(None, {"images": tensor})[0]
        inference_ms = (time.perf_counter() - started) * 1000.0
        people = _decode(output, frame.shape[1], frame.shape[0], scale, pad_x, pad_y)
        return DetectionFrame(people=people, inference_ms=inference_ms)


def _letterbox(
    frame: npt.NDArray[np.uint8],
) -> tuple[npt.NDArray[np.float32], float, float, float]:
    height, width = frame.shape[:2]
    if height <= 0 or width <= 0:
        raise ValueError(f"invalid frame shape: {frame.shape}")
    scale = min(MODEL_INPUT_SIZE / width, MODEL_INPUT_SIZE / height)
    resized_width = max(1, round(width * scale))
    resized_height = max(1, round(height * scale))
    resized = cv2.resize(frame, (resized_width, resized_height), interpolation=cv2.INTER_LINEAR)
    pad_x = (MODEL_INPUT_SIZE - resized_width) // 2
    pad_y = (MODEL_INPUT_SIZE - resized_height) // 2
    canvas = np.full((MODEL_INPUT_SIZE, MODEL_INPUT_SIZE, 3), 114, dtype=np.uint8)
    canvas[pad_y : pad_y + resized_height, pad_x : pad_x + resized_width] = resized
    rgb = canvas[:, :, ::-1]
    tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None], dtype=np.float32) / 255.0
    return tensor, scale, float(pad_x), float(pad_y)


def _decode(
    output: npt.NDArray[np.float32],
    image_width: int,
    image_height: int,
    scale: float,
    pad_x: float,
    pad_y: float,
) -> tuple[PersonPose, ...]:
    if output.shape != (1, 56, 2100):
        raise ValueError(f"unexpected pose output shape: {output.shape}")
    rows = output[0].T
    rows = rows[rows[:, 4] >= _DETECTION_CONFIDENCE]
    if not len(rows):
        return ()

    boxes = _xywh_to_xyxy(rows[:, :4])
    keep = _nms(boxes, rows[:, 4], _NMS_IOU, len(rows))
    people: list[PersonPose] = []
    for index in keep:
        row = rows[index]
        body = _normalized_box(boxes[index], image_width, image_height, scale, pad_x, pad_y)
        keypoints = _normalized_keypoints(row[5:], image_width, image_height, scale, pad_x, pad_y)
        head = _head_box(body, keypoints)
        if head is not None:
            people.append(PersonPose(body, head, float(row[4]), keypoints))
        if len(people) >= _MAX_PEOPLE:
            break
    return tuple(people)


def _xywh_to_xyxy(boxes: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    converted = boxes.copy()
    converted[:, 0] = boxes[:, 0] - boxes[:, 2] / 2
    converted[:, 1] = boxes[:, 1] - boxes[:, 3] / 2
    converted[:, 2] = boxes[:, 0] + boxes[:, 2] / 2
    converted[:, 3] = boxes[:, 1] + boxes[:, 3] / 2
    return converted


def _nms(
    boxes: npt.NDArray[np.float32],
    scores: npt.NDArray[np.float32],
    iou_threshold: float,
    limit: int,
) -> list[int]:
    order = np.argsort(scores)[::-1]
    keep: list[int] = []
    while len(order) and len(keep) < limit:
        chosen = int(order[0])
        keep.append(chosen)
        if len(order) == 1:
            break
        rest = order[1:]
        left = np.maximum(boxes[chosen, 0], boxes[rest, 0])
        top = np.maximum(boxes[chosen, 1], boxes[rest, 1])
        right = np.minimum(boxes[chosen, 2], boxes[rest, 2])
        bottom = np.minimum(boxes[chosen, 3], boxes[rest, 3])
        intersection = np.maximum(0.0, right - left) * np.maximum(0.0, bottom - top)
        chosen_area = max(0.0, float(boxes[chosen, 2] - boxes[chosen, 0])) * max(
            0.0, float(boxes[chosen, 3] - boxes[chosen, 1])
        )
        rest_area = np.maximum(0.0, boxes[rest, 2] - boxes[rest, 0]) * np.maximum(0.0, boxes[rest, 3] - boxes[rest, 1])
        union = chosen_area + rest_area - intersection
        iou = np.divide(intersection, union, out=np.zeros_like(intersection), where=union > 0.0)
        order = rest[iou < iou_threshold]
    return keep


def _normalized_box(
    box: npt.NDArray[np.float32],
    image_width: int,
    image_height: int,
    scale: float,
    pad_x: float,
    pad_y: float,
) -> FaceBox:
    x1 = _clamp((float(box[0]) - pad_x) / scale / image_width)
    y1 = _clamp((float(box[1]) - pad_y) / scale / image_height)
    x2 = _clamp((float(box[2]) - pad_x) / scale / image_width)
    y2 = _clamp((float(box[3]) - pad_y) / scale / image_height)
    return _box_from_edges(x1, y1, x2, y2)


def _normalized_keypoints(
    raw: npt.NDArray[np.float32],
    image_width: int,
    image_height: int,
    scale: float,
    pad_x: float,
    pad_y: float,
) -> tuple[Keypoint, ...]:
    points = raw.reshape(17, 3)
    return tuple(
        Keypoint(
            name,
            _clamp((float(point[0]) - pad_x) / scale / image_width),
            _clamp((float(point[1]) - pad_y) / scale / image_height),
            float(point[2]),
        )
        for name, point in zip(_KEYPOINT_NAMES, points, strict=True)
    )


def _head_box(body: FaceBox, keypoints: tuple[Keypoint, ...]) -> FaceBox | None:
    visible = {point.name: point for point in keypoints if point.visible}
    face = [visible[name] for name in _FACE_NAMES if name in visible]
    left_shoulder = visible.get(KeypointName.LEFT_SHOULDER)
    right_shoulder = visible.get(KeypointName.RIGHT_SHOULDER)
    nose = visible.get(KeypointName.NOSE)
    shoulders_visible = left_shoulder is not None and right_shoulder is not None
    shoulder_width = (
        abs(left_shoulder.x - right_shoulder.x)
        if left_shoulder is not None and right_shoulder is not None
        else body.width * 0.55
    )
    minimum_width = max(shoulder_width * 0.65, body.width * 0.24)

    if len(face) >= 2:
        left = min(point.x for point in face)
        right = max(point.x for point in face)
        top = min(point.y for point in face)
        bottom = max(point.y for point in face)
        center_x = (left + right) / 2
        center_y = (top + bottom) / 2
        width = max((right - left) * 1.8, minimum_width)
        height = max((bottom - top) * 2.2, width * 1.15, body.height * 0.16)
    elif nose is not None:
        center_x, center_y = nose.x, nose.y
        width = minimum_width
        height = max(width * 1.15, body.height * 0.16)
    elif shoulders_visible:
        assert left_shoulder is not None and right_shoulder is not None
        center_x = (left_shoulder.x + right_shoulder.x) / 2
        center_y = min(left_shoulder.y, right_shoulder.y) - body.height * 0.16
        width = minimum_width
        height = max(width * 1.15, body.height * 0.16)
    elif body.width >= _BODY_FALLBACK_MIN_WIDTH:
        width = body.width * 0.40
        height = max(width * 1.15, body.height * 0.16)
        body_top = body.center_y - body.height / 2
        return _centered_box(
            body.center_x,
            body_top + height / 2,
            width,
            height,
            lockable=False,
            subject_center_x=body.center_x,
            subject_center_y=body.center_y,
        )
    else:
        return None
    return _centered_box(
        center_x,
        center_y,
        width,
        height,
        subject_center_x=body.center_x,
        subject_center_y=body.center_y,
    )


def _centered_box(
    center_x: float,
    center_y: float,
    width: float,
    height: float,
    *,
    lockable: bool = True,
    subject_center_x: float | None = None,
    subject_center_y: float | None = None,
) -> FaceBox:
    half_width = min(width, 1.0) / 2
    half_height = min(height, 1.0) / 2
    center_x = min(max(center_x, half_width), 1.0 - half_width)
    center_y = min(max(center_y, half_height), 1.0 - half_height)
    return FaceBox(
        center_x,
        center_y,
        half_width * 2,
        half_height * 2,
        lockable=lockable,
        subject_center_x=subject_center_x,
        subject_center_y=subject_center_y,
    )


def _box_from_edges(left: float, top: float, right: float, bottom: float) -> FaceBox:
    width = max(0.0, right - left)
    height = max(0.0, bottom - top)
    return FaceBox(left + width / 2, top + height / 2, width, height)


def _clamp(value: float) -> float:
    return min(max(value, 0.0), 1.0)
