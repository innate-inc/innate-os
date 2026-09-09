# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The four models recognition runs on, each loaded on first use.

OpenCV's HOG pedestrian detector needs no file at all, so a checkout with no
weights still finds people and simply never names them. YuNet and SFace are
fetched once into ``data/models/people/``, pinned to a revision and a digest;
OSNet is the ONNX export in the tree under ``workspace/innate_skills/models``. Every heavyweight import (onnxruntime) happens inside the model that
needs it, so a missing library costs one embedder rather than the node.

PURE module: cv2 and numpy, no ROS.
"""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from brain_client.common.script_paths import get_innate_os_root

_ZOO = "https://github.com/opencv/opencv_zoo/raw/47534e27c9851bb1128ccc0102f1145e27f23f98/models"
_DOWNLOAD_TIMEOUT_SEC = 30.0
_OSNET_RELATIVE = Path("workspace/innate_skills/models/osnet_x0_25_msmt17.onnx")


@dataclass(frozen=True)
class ModelFile:
    """One downloadable weight file, pinned to a revision and a digest so the
    cache can never hold something other than what was reviewed."""

    filename: str
    url: str
    sha256: str


_YUNET = ModelFile(
    "face_detection_yunet_2023mar.onnx",
    f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
)
_SFACE = ModelFile(
    "face_recognition_sface_2021dec.onnx",
    f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
    "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
)

Box = tuple[float, float, float, float]
"""``(ymin, xmin, ymax, xmax)`` of the frame, normalized — resolution-free."""


@dataclass(frozen=True)
class FaceHit:
    """One face inside a crop, in that crop's pixels, with YuNet's five points."""

    x: float
    y: float
    w: float
    h: float
    landmarks: tuple[tuple[float, float], ...]
    score: float


def models_dir() -> Path:
    return get_innate_os_root() / "data" / "models" / "people"


class PersonDetector:
    """OpenCV's HOG + linear SVM pedestrian detector: no model file, no GPU."""

    def __init__(self, *, min_weight: float = 0.4, detect_width: int = 320) -> None:
        hog = cv2.HOGDescriptor()
        set_detector: Any = hog.setSVMDetector  # the stubs say UMat; the runtime takes the float vector
        set_detector(cv2.HOGDescriptor.getDefaultPeopleDetector())
        self._hog = hog
        self._min_weight = min_weight
        self._detect_width = detect_width

    def detect(self, frame_bgr: np.ndarray) -> list[Box]:
        height, width = frame_bgr.shape[:2]
        if not width or not height:
            return []
        scale = min(1.0, self._detect_width / width)
        image = frame_bgr if scale >= 1.0 else cv2.resize(frame_bgr, (self._detect_width, round(height * scale)))
        rects, weights = self._hog.detectMultiScale(image, winStride=(8, 8), padding=(8, 8), scale=1.05)
        rows, columns = image.shape[:2]
        return [
            (y / rows, x / columns, (y + h) / rows, (x + w) / columns)
            for (x, y, w, h), weight in zip(rects, np.asarray(weights).reshape(-1), strict=False)
            if float(weight) >= self._min_weight
        ]


class FaceLocator:
    """YuNet 2023mar (OpenCV Zoo, MIT): boxes and five landmarks per crop."""

    def __init__(self, model_path: Path, *, score_threshold: float = 0.6) -> None:
        self._model_path = model_path
        self._score_threshold = score_threshold
        self._detector: cv2.FaceDetectorYN | None = None
        self._size = (0, 0)

    def locate(self, crop_bgr: np.ndarray) -> list[FaceHit]:
        if crop_bgr.size == 0:
            return []
        height, width = crop_bgr.shape[:2]
        detector = self._ensure(width, height)
        if detector is None:
            return []
        _count, faces = detector.detect(crop_bgr)
        return [] if faces is None else [_hit(np.asarray(row, dtype=np.float32)) for row in faces]

    def _ensure(self, width: int, height: int) -> cv2.FaceDetectorYN | None:
        if self._detector is None:
            try:
                self._detector = cv2.FaceDetectorYN.create(
                    str(self._model_path), "", (width, height), self._score_threshold, 0.3, 20
                )
            except cv2.error:
                return None
        elif self._size != (width, height):
            self._detector.setInputSize((width, height))
        self._size = (width, height)
        return self._detector


class FaceEmbedder:
    """SFace 2021dec (OpenCV Zoo, Apache-2.0): 128-d off a crop aligned on YuNet's landmarks."""

    def __init__(self, model_path: Path) -> None:
        self._model_path = model_path
        self._recognizer: cv2.FaceRecognizerSF | None = None

    def embed(self, crop_bgr: np.ndarray, hit: FaceHit) -> np.ndarray | None:
        recognizer = self._ensure()
        if recognizer is None or crop_bgr.size == 0:
            return None
        aligned = recognizer.alignCrop(crop_bgr, _row(hit))
        return _unit(np.asarray(recognizer.feature(aligned), dtype=np.float32).reshape(-1))

    def _ensure(self) -> cv2.FaceRecognizerSF | None:
        if self._recognizer is None:
            try:
                self._recognizer = cv2.FaceRecognizerSF.create(str(self._model_path), "")
            except cv2.error:
                return None
        return self._recognizer


class OutfitEmbedder:
    """OSNet x0.25 MSMT17 (MIT): 512-d off a 256x128 body crop, through onnxruntime."""

    def __init__(self, model_path: Path) -> None:
        self._model_path = model_path
        self._session: Any = None
        self._failed = False
        self._input_name = ""

    def embed(self, crop_bgr: np.ndarray) -> np.ndarray | None:
        session = self._ensure()
        if session is None or crop_bgr.size == 0:
            return None
        rgb = cv2.cvtColor(cv2.resize(crop_bgr, (128, 256)), cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        tensor = ((rgb - mean) / std).transpose(2, 0, 1)[None, ...]
        return _unit(np.asarray(session.run(None, {self._input_name: tensor})[0], dtype=np.float32).reshape(-1))

    def _ensure(self) -> Any:
        if self._session is not None or self._failed:
            return self._session
        try:
            onnxruntime = import_module("onnxruntime")  # deferred: ~200 MiB of native library
            session = onnxruntime.InferenceSession(str(self._model_path), providers=["CPUExecutionProvider"])
            self._input_name = session.get_inputs()[0].name
        except (ImportError, OSError, RuntimeError, IndexError):
            self._failed = True
            return None
        self._session = session
        return session


@dataclass(frozen=True)
class Models:
    detector: PersonDetector
    face_locator: FaceLocator | None
    face: FaceEmbedder | None
    outfit: OutfitEmbedder | None

    @property
    def health(self) -> dict[str, str]:
        return {
            "faces": "ok" if self.face is not None else "unavailable",
            "outfits": "ok" if self.outfit is not None else "unavailable",
        }


def load_models(*, allow_download: bool = True) -> Models:
    """The best stack available, reporting what is missing rather than failing:
    with no face model the robot still sees people, it just never names them."""
    directory = models_dir()
    detector_path = _fetch(_YUNET, directory, allow_download=allow_download)
    embedder_path = _fetch(_SFACE, directory, allow_download=allow_download)
    faces = detector_path is not None and embedder_path is not None
    osnet = get_innate_os_root() / _OSNET_RELATIVE
    return Models(
        detector=PersonDetector(),
        face_locator=FaceLocator(detector_path) if faces and detector_path else None,
        face=FaceEmbedder(embedder_path) if faces and embedder_path else None,
        outfit=OutfitEmbedder(osnet) if osnet.exists() else None,
    )


def _fetch(model: ModelFile, directory: Path, *, allow_download: bool) -> Path | None:
    """The file on disk with the digest it was pinned at, fetched once if it
    is missing or corrupt. None when it cannot be had — the caller degrades."""
    path = directory / model.filename
    if path.exists() and _digest(path.read_bytes()) == model.sha256:
        return path
    if not allow_download:
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(model.url, timeout=_DOWNLOAD_TIMEOUT_SEC) as response:
            payload = response.read()
        if _digest(payload) != model.sha256:
            return None
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return path


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def crop(frame_bgr: np.ndarray, box: Box) -> np.ndarray:
    height, width = frame_bgr.shape[:2]
    top, left = max(0, round(box[0] * height)), max(0, round(box[1] * width))
    bottom, right = min(height, round(box[2] * height)), min(width, round(box[3] * width))
    return frame_bgr[top:bottom, left:right] if bottom > top and right > left else np.empty((0, 0, 3), np.uint8)


def _unit(vector: np.ndarray) -> np.ndarray | None:
    norm = float(np.linalg.norm(vector))
    return None if norm < 1e-9 else (vector / norm).astype(np.float32)


def _hit(row: np.ndarray) -> FaceHit:
    """YuNet's 15-column row. It orders the eyes and mouth corners right-then-left."""
    points = tuple((float(row[i]), float(row[i + 1])) for i in (6, 4, 8, 12, 10))
    return FaceHit(
        x=float(row[0]), y=float(row[1]), w=float(row[2]), h=float(row[3]), landmarks=points, score=float(row[14])
    )


def _row(hit: FaceHit) -> np.ndarray:
    """The inverse of :func:`_hit`, which is what ``alignCrop`` wants."""
    left_eye, right_eye, nose, left_mouth, right_mouth = hit.landmarks
    return np.array(
        [hit.x, hit.y, hit.w, hit.h, *right_eye, *left_eye, *nose, *right_mouth, *left_mouth, hit.score],
        dtype=np.float32,
    ).reshape(1, -1)
