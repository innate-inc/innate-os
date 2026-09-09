# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The interchangeable models behind the engine's four protocols.

Every heavyweight import (onnxruntime) happens inside the backend
that needs it, on first use, never at module import — a missing library
degrades to a health flag and a working engine rather than a node that will not
start. The zero-download path (OpenCV's own HOG person detector, no face model)
always loads, which is what the simulator and a fresh checkout run on.

Model files live under ``data/models/people/``. They are packaged at
provisioning (RFC 12) so a robot never downloads weights at startup; the fetch
here is the developer convenience for a checkout that has none, and it happens
on first use, not at import.
"""

from __future__ import annotations

import hashlib
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from brain_client.common.enums import StrEnum
from brain_client.common.script_paths import get_innate_os_root
from brain_client.people.types import Detection, FaceHit, HealthDict, HealthState

if TYPE_CHECKING:
    from brain_client.people.types import BodyEmbedder, FaceEmbedder, FaceLocator, PersonDetector

_ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"
_DOWNLOAD_TIMEOUT_SEC = 30.0


class Backend(StrEnum):
    """Which face stack to prefer; wire-visible (a node parameter)."""

    OPENCV = "opencv"
    NONE = "none"


@dataclass(frozen=True)
class ModelAsset:
    filename: str
    url: str
    # The OpenCV Zoo publishes no per-file checksum for these two; the field
    # exists so a pinned hash can be dropped in the day it does, and so the
    # provisioning image can pin what it shipped.
    sha256: str | None = None


YUNET = ModelAsset(
    "face_detection_yunet_2023mar.onnx", f"{_ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
SFACE = ModelAsset(
    "face_recognition_sface_2021dec.onnx", f"{_ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx"
)
OSNET_RELATIVE = Path("workspace/innate_skills/models/osnet_x0_25_msmt17.onnx")


def default_models_dir() -> Path:
    return get_innate_os_root() / "data" / "models" / "people"


def ensure_model(asset: ModelAsset, directory: Path, *, allow_download: bool = True) -> Path | None:
    """The asset's path on disk, fetching it once if it is missing. None when it
    is absent and cannot be fetched — the caller degrades, it never raises."""
    path = directory / asset.filename
    if path.exists() and _matches(path, asset.sha256):
        return path
    if not allow_download:
        return None
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(asset.url, timeout=_DOWNLOAD_TIMEOUT_SEC) as response:
            payload = response.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not _matches_bytes(payload, asset.sha256):
        return None
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    except OSError:
        return None
    return path


def _matches(path: Path, sha256: str | None) -> bool:
    if sha256 is None:
        return True
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() == sha256
    except OSError:
        return False


def _matches_bytes(payload: bytes, sha256: str | None) -> bool:
    return sha256 is None or hashlib.sha256(payload).hexdigest() == sha256


class HogPersonDetector:
    """OpenCV's own HOG + linear SVM pedestrian detector: no model file, no
    download, no GPU. The simulator default and the fallback whenever the
    TensorRT detector of RFC 4.2 is unavailable."""

    name = "hog"

    def __init__(self, *, min_score: float = 0.35, detect_width: int = 320) -> None:
        self._hog = _people_hog()
        self._min_score = min_score
        self._detect_width = detect_width

    def detect(self, frame_bgr: np.ndarray) -> list[Detection]:
        height, width = frame_bgr.shape[:2]
        if width == 0 or height == 0:
            return []
        scale = min(1.0, self._detect_width / width)
        image = frame_bgr if scale >= 1.0 else cv2.resize(frame_bgr, (self._detect_width, round(height * scale)))
        rects, weights = self._hog.detectMultiScale(image, winStride=(8, 8), padding=(8, 8), scale=1.05)
        found: list[Detection] = []
        for (x, y, w, h), weight in zip(rects, np.asarray(weights).reshape(-1), strict=False):
            score = _hog_score(float(weight))
            if score < self._min_score:
                continue
            ih, iw = image.shape[:2]
            found.append(
                Detection(
                    box=(y / ih, x / iw, (y + h) / ih, (x + w) / iw),
                    score=score,
                    source="body",
                )
            )
        return found


def _people_hog() -> cv2.HOGDescriptor:
    # cv2's stubs type setSVMDetector as taking a UMat; the runtime takes the
    # plain float vector getDefaultPeopleDetector returns, and nothing else.
    hog = cv2.HOGDescriptor()
    set_detector: Any = hog.setSVMDetector
    set_detector(cv2.HOGDescriptor.getDefaultPeopleDetector())
    return hog


def _hog_score(weight: float) -> float:
    """HOG returns an SVM margin, not a probability. This maps the useful band
    (0 to ~1.5) onto the 0-1 confidence the rest of the engine expects."""
    return max(0.0, min(1.0, 0.35 + 0.3 * weight))


class YuNetFaceLocator:
    """YuNet 2023mar (OpenCV Zoo, MIT, 76 k params) through ``cv2.FaceDetectorYN``.

    Five landmarks come free with every box, which is where the engine's yaw and
    pitch come from. The session is created on the first crop, not in __init__,
    so a node with no model file still starts.
    """

    name = "yunet-2023mar"

    def __init__(self, model_path: Path, *, score_threshold: float = 0.6, nms: float = 0.3, top_k: int = 20) -> None:
        self._model_path = model_path
        self._score_threshold = score_threshold
        self._nms = nms
        self._top_k = top_k
        self._detector: cv2.FaceDetectorYN | None = None
        self._size: tuple[int, int] = (0, 0)

    def locate(self, crop_bgr: np.ndarray) -> list[FaceHit]:
        if crop_bgr.size == 0:
            return []
        height, width = crop_bgr.shape[:2]
        detector = self._ensure(width, height)
        if detector is None:
            return []
        _count, faces = detector.detect(crop_bgr)
        if faces is None:
            return []
        return [_hit_from_row(np.asarray(row, dtype=np.float32)) for row in faces]

    def _ensure(self, width: int, height: int) -> cv2.FaceDetectorYN | None:
        if self._detector is None:
            try:
                self._detector = cv2.FaceDetectorYN.create(
                    str(self._model_path), "", (width, height), self._score_threshold, self._nms, self._top_k
                )
            except cv2.error:
                return None
            self._size = (width, height)
            return self._detector
        if self._size != (width, height):
            self._detector.setInputSize((width, height))
            self._size = (width, height)
        return self._detector


class SFaceEmbedder:
    """SFace 2021dec (OpenCV Zoo, Apache-2.0): 128-d from a 112x112 crop aligned
    on YuNet's landmarks, through ``cv2.FaceRecognizerSF``."""

    model = "sface-2021dec-128"

    def __init__(self, model_path: Path) -> None:
        self._model_path = model_path
        self._recognizer: cv2.FaceRecognizerSF | None = None

    def embed(self, crop_bgr: np.ndarray, hit: FaceHit) -> np.ndarray:
        recognizer = self._ensure()
        if recognizer is None or crop_bgr.size == 0:
            return np.zeros(0, dtype=np.float32)
        aligned = recognizer.alignCrop(crop_bgr, _row_from_hit(hit))
        return _l2(np.asarray(recognizer.feature(aligned), dtype=np.float32).reshape(-1))

    def _ensure(self) -> cv2.FaceRecognizerSF | None:
        if self._recognizer is None:
            try:
                self._recognizer = cv2.FaceRecognizerSF.create(str(self._model_path), "")
            except cv2.error:
                return None
        return self._recognizer


class OsnetBodyEmbedder:
    """OSNet x0.25 MSMT17 (MIT), 512-d, through onnxruntime on a 256x128 crop.

    The ONNX export already in the tree under ``workspace/innate_skills/models``;
    onnxruntime is imported on first use.
    """

    model = "osnet-x0_25-msmt17-512"

    def __init__(self, model_path: Path) -> None:
        self._model_path = model_path
        self._session: Any = None
        self._input_name = ""
        self._failed = False

    @property
    def available(self) -> bool:
        return self._model_path.exists()

    def embed(self, crop_bgr: np.ndarray) -> np.ndarray:
        session = self._ensure()
        if session is None or crop_bgr.size == 0:
            return np.zeros(0, dtype=np.float32)
        resized = cv2.resize(crop_bgr, (128, 256), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
        std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
        tensor = ((rgb - mean) / std).transpose(2, 0, 1)[None, ...]
        outputs = session.run(None, {self._input_name: tensor})
        return _l2(np.asarray(outputs[0], dtype=np.float32).reshape(-1))

    def _ensure(self) -> Any:
        if self._session is not None or self._failed:
            return self._session
        try:
            # Deferred: ~200 MiB of native library the zero-model fallback must not pay for.
            onnxruntime = import_module("onnxruntime")
        except ImportError:
            self._failed = True
            return None
        try:
            session = onnxruntime.InferenceSession(str(self._model_path), providers=["CPUExecutionProvider"])
            self._input_name = session.get_inputs()[0].name
        except (OSError, RuntimeError, IndexError):
            self._failed = True
            return None
        self._session = session
        return session


class NullBodyEmbedder:
    """No body model: every crop embeds to nothing, so outfit evidence is simply
    absent and the engine runs on faces and continuity alone."""

    model = ""

    def embed(self, crop_bgr: np.ndarray) -> np.ndarray:
        del crop_bgr
        return np.zeros(0, dtype=np.float32)


@dataclass
class Backends:
    """The models one engine runs on, plus what the snapshot says about them."""

    detector: PersonDetector
    locator: FaceLocator | None
    face: FaceEmbedder | None
    body: BodyEmbedder | None
    health: HealthDict = field(default_factory=lambda: HealthDict())

    @property
    def face_model(self) -> str:
        return self.face.model if self.face is not None else ""

    @property
    def body_model(self) -> str:
        return self.body.model if self.body is not None else ""


def load_backends(
    prefer: str = Backend.OPENCV,
    models_dir: Path | None = None,
    *,
    allow_download: bool = True,
) -> Backends:
    """Assemble the best available stack, reporting what is missing rather than
    failing. Never raises: a robot with no model files still tracks people."""
    directory = models_dir or default_models_dir()
    detector = HogPersonDetector()
    locator, face, face_health = _load_face(prefer, directory, allow_download=allow_download)
    body, body_health = _load_body()
    health = HealthDict(
        camera=str(HealthState.UNAVAILABLE),
        native=str(HealthState.UNAVAILABLE),
        face_model=str(face_health),
        body_model=str(body_health),
        gpu=str(HealthState.NONE),  # TensorRT arrives with the Phase 2 detector
    )
    return Backends(detector=detector, locator=locator, face=face, body=body, health=health)


def _load_face(
    prefer: str, directory: Path, *, allow_download: bool
) -> tuple[FaceLocator | None, FaceEmbedder | None, HealthState]:
    if prefer == Backend.NONE:
        return (None, None, HealthState.NONE)
    detector_path = ensure_model(YUNET, directory, allow_download=allow_download)
    embedder_path = ensure_model(SFACE, directory, allow_download=allow_download)
    if detector_path is None or embedder_path is None:
        return (None, None, HealthState.UNAVAILABLE)
    return (YuNetFaceLocator(detector_path), SFaceEmbedder(embedder_path), HealthState.OK)


def _load_body() -> tuple[BodyEmbedder, HealthState]:
    path = get_innate_os_root() / OSNET_RELATIVE
    if not path.exists():
        return (NullBodyEmbedder(), HealthState.UNAVAILABLE)
    return (OsnetBodyEmbedder(path), HealthState.OK)


def _l2(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector if norm < 1e-9 else (vector / norm).astype(np.float32)


def _hit_from_row(row: np.ndarray) -> FaceHit:
    """YuNet's 15-column row -> FaceHit. The row orders the eyes and mouth
    corners right-then-left; FaceHit's contract is left-then-right."""
    return FaceHit(
        x=float(row[0]),
        y=float(row[1]),
        w=float(row[2]),
        h=float(row[3]),
        landmarks=(
            (float(row[6]), float(row[7])),
            (float(row[4]), float(row[5])),
            (float(row[8]), float(row[9])),
            (float(row[12]), float(row[13])),
            (float(row[10]), float(row[11])),
        ),
        score=float(row[14]),
    )


def _row_from_hit(hit: FaceHit) -> np.ndarray:
    """The inverse of :func:`_hit_from_row`, which is what alignCrop wants."""
    points = list(hit.landmarks) if len(hit.landmarks) == 5 else [(0.0, 0.0)] * 5
    left_eye, right_eye, nose, left_mouth, right_mouth = points[0], points[1], points[2], points[3], points[4]
    return np.array(
        [
            hit.x,
            hit.y,
            hit.w,
            hit.h,
            right_eye[0],
            right_eye[1],
            left_eye[0],
            left_eye[1],
            nose[0],
            nose[1],
            right_mouth[0],
            right_mouth[1],
            left_mouth[0],
            left_mouth[1],
            hit.score,
        ],
        dtype=np.float32,
    ).reshape(1, -1)
