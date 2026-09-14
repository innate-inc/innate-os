# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
# Exact AST definitions extracted from PR #807; synthetic benchmark only.
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import cv2
import numpy as np
Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class FaceHit:
    """One face inside a crop, in that crop's pixels, with YuNet's five points."""
    x: float
    y: float
    w: float
    h: float
    landmarks: tuple[tuple[float, float], ...]
    score: float

class PersonDetector:
    """OpenCV's HOG + linear SVM pedestrian detector: no model file, no GPU."""

    def __init__(self, *, min_weight: float=0.4, detect_width: int=320) -> None:
        hog = cv2.HOGDescriptor()
        set_detector: Any = hog.setSVMDetector
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
        return [(y / rows, x / columns, (y + h) / rows, (x + w) / columns) for (x, y, w, h), weight in zip(rects, np.asarray(weights).reshape(-1), strict=False) if float(weight) >= self._min_weight]

class FaceLocator:
    """YuNet 2023mar (OpenCV Zoo, MIT): boxes and five landmarks per crop."""

    def __init__(self, model_path: Path, *, score_threshold: float=0.6) -> None:
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
                self._detector = cv2.FaceDetectorYN.create(str(self._model_path), '', (width, height), self._score_threshold, 0.3, 20)
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
                self._recognizer = cv2.FaceRecognizerSF.create(str(self._model_path), '')
            except cv2.error:
                return None
        return self._recognizer

def crop(frame_bgr: np.ndarray, box: Box) -> np.ndarray:
    height, width = frame_bgr.shape[:2]
    top, left = (max(0, round(box[0] * height)), max(0, round(box[1] * width)))
    bottom, right = (min(height, round(box[2] * height)), min(width, round(box[3] * width)))
    return frame_bgr[top:bottom, left:right] if bottom > top and right > left else np.empty((0, 0, 3), np.uint8)

def _unit(vector: np.ndarray) -> np.ndarray | None:
    norm = float(np.linalg.norm(vector))
    return None if norm < 1e-09 else (vector / norm).astype(np.float32)

def _hit(row: np.ndarray) -> FaceHit:
    """YuNet's 15-column row. It orders the eyes and mouth corners right-then-left."""
    points = tuple(((float(row[i]), float(row[i + 1])) for i in (6, 4, 8, 12, 10)))
    return FaceHit(x=float(row[0]), y=float(row[1]), w=float(row[2]), h=float(row[3]), landmarks=points, score=float(row[14]))

def _row(hit: FaceHit) -> np.ndarray:
    """The inverse of :func:`_hit`, which is what ``alignCrop`` wants."""
    left_eye, right_eye, nose, left_mouth, right_mouth = hit.landmarks
    return np.array([hit.x, hit.y, hit.w, hit.h, *right_eye, *left_eye, *nose, *right_mouth, *left_mouth, hit.score], dtype=np.float32).reshape(1, -1)

def _best(gallery: list[tuple[str, np.ndarray]], vector: np.ndarray, accept: float, margin: float) -> str | None:
    """The one person this vector belongs to, or None when the gallery cannot
    say: nobody above ``accept``, or two people too close to call apart."""
    scores: dict[str, float] = {}
    for person_id, stored in gallery:
        score = float(stored @ vector)
        scores[person_id] = max(score, scores.get(person_id, -1.0))
    if not scores:
        return None
    ranked = sorted(scores.values(), reverse=True)
    best_id = max(scores, key=lambda key: scores[key])
    runner_up = ranked[1] if len(ranked) > 1 else -1.0
    return best_id if ranked[0] >= accept and ranked[0] - runner_up >= margin else None
