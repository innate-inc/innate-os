# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Local embedding backends and conservative face/body score fusion."""

from __future__ import annotations

import math
import threading
from pathlib import Path
from typing import Any

import numpy as np

BODY_MATCH_THRESHOLD = 0.78
BODY_PLAUSIBLE_THRESHOLD = 0.68
BODY_MARGIN = 0.07
FACE_MATCH_THRESHOLD = 0.50
FACE_PLAUSIBLE_THRESHOLD = 0.38
FACE_MARGIN = 0.06
MODEL_PATH = Path(__file__).with_name("models") / "osnet_x0_25_msmt17.onnx"
FACE_TILE_FRACTION = 0.65
FACE_TILE_SCALE = 1.5


def serialized_embedding(vector: Any) -> list[float] | None:
    normalized = _unit(vector)
    if normalized is None:
        return None
    return [round(float(value), 6) for value in normalized]


def _unit(values: Any) -> np.ndarray | None:
    try:
        vector = np.asarray(values, dtype=np.float32).reshape(-1)
    except (TypeError, ValueError):
        return None
    norm = float(np.linalg.norm(vector))
    if not vector.size or not math.isfinite(norm) or norm <= 1e-8:
        return None
    return vector / norm


def cosine_similarity(left: Any, right: Any) -> float | None:
    left_vector, right_vector = _unit(left), _unit(right)
    if (
        left_vector is None
        or right_vector is None
        or left_vector.shape != right_vector.shape
    ):
        return None
    return float(np.clip(left_vector @ right_vector, -1.0, 1.0))


class LocalPersonEncoder:
    """One warm local body encoder plus an optional InspireFace session."""

    def __init__(self, model_path: Path = MODEL_PATH):
        self._body_session = None
        self._face_session = None
        self._body_error: str | None = None
        self._face_error: str | None = None
        self.providers: list[str] = []
        self._load_body(model_path)
        self._load_face()

    def _load_body(self, model_path: Path) -> None:
        try:
            import onnxruntime as ort

            available = ort.get_available_providers()
            preferred = [
                provider
                for provider in (
                    "TensorrtExecutionProvider",
                    "CUDAExecutionProvider",
                    "CPUExecutionProvider",
                )
                if provider in available
            ]
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            self._body_session = ort.InferenceSession(
                str(model_path),
                sess_options=options,
                providers=preferred or None,
            )
            self.providers = self._body_session.get_providers()
        except Exception as error:  # noqa: BLE001 - optional native backend has provider-specific errors
            self._body_error = f"{type(error).__name__}: {error}"

    def _load_face(self) -> None:
        try:
            import inspireface as isf

            parameter = isf.SessionCustomParameter(enable_recognition=True)
            self._face_session = isf.InspireFaceSession(
                param=parameter,
                detect_mode=isf.HF_DETECT_MODE_ALWAYS_DETECT,
                max_detect_num=3,
            )
            self._face_session.set_detection_confidence_threshold(0.4)
        except Exception as error:  # noqa: BLE001 - optional native SDK raises ctypes-specific errors
            self._face_error = f"{type(error).__name__}: {error}"

    @property
    def available(self) -> bool:
        return self._body_session is not None or self._face_session is not None

    @property
    def face_available(self) -> bool:
        """Whether the face-recognition backend initialized successfully."""
        return self._face_session is not None

    @property
    def diagnostics(self) -> dict[str, Any]:
        return {
            "body_available": self._body_session is not None,
            "body_providers": self.providers,
            "body_error": self._body_error,
            "face_available": self._face_session is not None,
            "face_error": self._face_error,
        }

    @staticmethod
    def _decode(jpeg: bytes):
        import cv2

        return cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)

    def _body_embedding(self, frame) -> np.ndarray | None:
        if self._body_session is None:
            return None
        import cv2

        rgb = cv2.cvtColor(
            cv2.resize(frame, (128, 256), interpolation=cv2.INTER_AREA),
            cv2.COLOR_BGR2RGB,
        )
        tensor = rgb.astype(np.float32) / 255.0
        tensor = (
            tensor - np.asarray((0.485, 0.456, 0.406), dtype=np.float32)
        ) / np.asarray((0.229, 0.224, 0.225), dtype=np.float32)
        tensor = np.transpose(tensor, (2, 0, 1))[None]
        input_name = self._body_session.get_inputs()[0].name
        output = self._body_session.run(None, {input_name: tensor})[0][0]
        return _unit(output)

    def _detect_face(self, frame):
        if self._face_session is None:
            return None, None, None
        import cv2

        faces = self._face_session.face_detection(frame)
        if faces:
            face = max(faces, key=self._face_area)
            return frame, face, tuple(float(value) for value in face.location)

        height, width = frame.shape[:2]
        tile_height = max(1, int(round(height * FACE_TILE_FRACTION)))
        tile_width = max(1, int(round(width * FACE_TILE_FRACTION)))
        best = None
        for offset_y in (0, height - tile_height):
            for offset_x in (0, width - tile_width):
                tile = frame[
                    offset_y : offset_y + tile_height,
                    offset_x : offset_x + tile_width,
                ]
                detection_frame = cv2.resize(
                    tile,
                    None,
                    fx=FACE_TILE_SCALE,
                    fy=FACE_TILE_SCALE,
                    interpolation=cv2.INTER_CUBIC,
                )
                for face in self._face_session.face_detection(detection_frame):
                    bounds = (
                        float(face.location[0]) / FACE_TILE_SCALE + offset_x,
                        float(face.location[1]) / FACE_TILE_SCALE + offset_y,
                        float(face.location[2]) / FACE_TILE_SCALE + offset_x,
                        float(face.location[3]) / FACE_TILE_SCALE + offset_y,
                    )
                    area = max(0.0, bounds[2] - bounds[0]) * max(
                        0.0, bounds[3] - bounds[1]
                    )
                    if best is None or area > best[0]:
                        best = (area, detection_frame, face, bounds)
        if best is None:
            return None, None, None
        return best[1], best[2], best[3]

    @staticmethod
    def _face_area(face) -> int:
        return max(0, face.location[2] - face.location[0]) * max(
            0, face.location[3] - face.location[1]
        )

    @staticmethod
    def _person_crop_from_face(frame, bounds):
        """Estimate a body crop from a detected face, clamped to the image."""
        if bounds is None:
            return frame
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = bounds
        face_width = max(1.0, x2 - x1)
        face_height = max(1.0, y2 - y1)
        center_x = (x1 + x2) / 2.0
        crop_x1 = max(0, int(round(center_x - 2.5 * face_width)))
        crop_x2 = min(width, int(round(center_x + 2.5 * face_width)))
        crop_y1 = max(0, int(round(y1 - 0.75 * face_height)))
        crop_y2 = min(height, int(round(y2 + 8.0 * face_height)))
        if crop_x2 <= crop_x1 or crop_y2 <= crop_y1:
            return frame
        return frame[crop_y1:crop_y2, crop_x1:crop_x2]

    def _face_embedding(self, detection_frame, face) -> np.ndarray | None:
        if self._face_session is None or detection_frame is None or face is None:
            return None
        return _unit(self._face_session.face_feature_extract(detection_frame, face))

    def encode(self, jpeg: bytes) -> dict[str, list[float] | None]:
        frame = self._decode(jpeg)
        if frame is None:
            raise ValueError("camera image is not a readable JPEG")
        detection_frame, face, face_bounds = self._detect_face(frame)
        body_frame = self._person_crop_from_face(frame, face_bounds)
        return {
            "body": serialized_embedding(self._body_embedding(body_frame)),
            "face": serialized_embedding(self._face_embedding(detection_frame, face)),
        }


_ENCODER: LocalPersonEncoder | None = None
_ENCODER_LOCK = threading.Lock()


def shared_encoder() -> LocalPersonEncoder:
    """Keep model sessions warm across skill calls in the skills server."""
    global _ENCODER
    with _ENCODER_LOCK:
        if _ENCODER is None:
            _ENCODER = LocalPersonEncoder()
        return _ENCODER


def profile_scores(
    profiles: list[dict[str, Any]], embeddings: dict[str, Any]
) -> list[dict[str, Any]]:
    scores: list[dict[str, Any]] = []
    for profile in profiles:
        face_scores: list[float] = []
        body_scores: list[float] = []
        for view in profile.get("views", []):
            face = cosine_similarity(embeddings.get("face"), view.get("face"))
            body = cosine_similarity(embeddings.get("body"), view.get("body"))
            if face is not None:
                face_scores.append(face)
            if body is not None:
                body_scores.append(body)
        scores.append(
            {
                "encounter_id": profile["encounter_id"],
                "face_score": max(face_scores) if face_scores else None,
                "body_score": max(body_scores) if body_scores else None,
            }
        )
    return scores


def _rank(
    scores: list[dict[str, Any]], key: str
) -> tuple[dict[str, Any] | None, float]:
    ranked = sorted(
        (entry for entry in scores if isinstance(entry.get(key), (int, float))),
        key=lambda entry: float(entry[key]),
        reverse=True,
    )
    if not ranked:
        return None, 0.0
    runner_up = float(ranked[1][key]) if len(ranked) > 1 else -1.0
    return ranked[0], float(ranked[0][key]) - runner_up


def identity_decision(
    scores: list[dict[str, Any]],
) -> tuple[str, str | None, list[str], dict[str, Any]]:
    face_best, face_margin = _rank(scores, "face_score")
    body_best, body_margin = _rank(scores, "body_score")
    face_match = (
        face_best
        if face_best is not None
        and face_best["face_score"] >= FACE_MATCH_THRESHOLD
        and face_margin >= FACE_MARGIN
        else None
    )
    body_match = (
        body_best
        if body_best is not None
        and body_best["body_score"] >= BODY_MATCH_THRESHOLD
        and body_margin >= BODY_MARGIN
        else None
    )
    evidence = {
        "face_score": round(float(face_best["face_score"]), 3)
        if face_best is not None
        else None,
        "body_score": round(float(body_best["body_score"]), 3)
        if body_best is not None
        else None,
        "face_margin": round(face_margin, 3) if face_best is not None else None,
        "body_margin": round(body_margin, 3) if body_best is not None else None,
    }
    if face_match is not None and body_match is not None:
        if face_match["encounter_id"] == body_match["encounter_id"]:
            return "known", face_match["encounter_id"], [], evidence
        plausible = sorted({face_match["encounter_id"], body_match["encounter_id"]})
        return "ambiguous", None, plausible, evidence
    if face_match is not None:
        return "known", face_match["encounter_id"], [], evidence
    if body_match is not None:
        return "known", body_match["encounter_id"], [], evidence
    plausible = sorted(
        {
            entry["encounter_id"]
            for entry in scores
            if (
                entry.get("face_score") is not None
                and entry["face_score"] >= FACE_PLAUSIBLE_THRESHOLD
            )
            or (
                entry.get("body_score") is not None
                and entry["body_score"] >= BODY_PLAUSIBLE_THRESHOLD
            )
        }
    )
    return ("ambiguous" if plausible else "unknown"), None, plausible, evidence
