"""Scripted backends for the engine and node tests: no model files, no cv2 DNN,
deterministic embeddings."""

from __future__ import annotations

import numpy as np

from brain_client.people.backends import Backends
from brain_client.people.types import Detection, FaceHit, HealthDict, HealthState


class FixedDetector:
    """Returns whatever the test scripted, one list per call then the last one."""

    name = "fixed"

    def __init__(self, frames: list[list[Detection]] | None = None) -> None:
        self.frames = frames or []
        self.calls = 0

    def detect(self, frame_bgr: np.ndarray) -> list[Detection]:
        del frame_bgr
        index = min(self.calls, len(self.frames) - 1) if self.frames else -1
        self.calls += 1
        return list(self.frames[index]) if index >= 0 else []


class CenterFaceLocator:
    """One face in the middle of every crop, sized to the crop — enough for the
    engine's plumbing without a model file."""

    name = "center"

    def __init__(self, *, fraction: float = 0.6, score: float = 0.9) -> None:
        self._fraction = fraction
        self._score = score

    def locate(self, crop_bgr: np.ndarray) -> list[FaceHit]:
        if crop_bgr.size == 0:
            return []
        height, width = crop_bgr.shape[:2]
        w, h = width * self._fraction, height * self._fraction
        x, y = (width - w) / 2.0, (height - h) / 2.0
        eye_y = y + h * 0.35
        return [
            FaceHit(
                x=x,
                y=y,
                w=w,
                h=h,
                landmarks=(
                    (x + w * 0.3, eye_y),
                    (x + w * 0.7, eye_y),
                    (x + w * 0.5, y + h * 0.55),
                    (x + w * 0.35, y + h * 0.75),
                    (x + w * 0.65, y + h * 0.75),
                ),
                score=self._score,
            )
        ]


class ColorFaceEmbedder:
    """A deterministic stand-in: the crop's mean colour as a unit vector. The
    same painted person embeds identically frame to frame and two differently
    painted ones are far apart, which is all a plumbing test needs."""

    model = "fake-face-4"

    def embed(self, crop_bgr: np.ndarray, hit: FaceHit) -> np.ndarray:
        del hit
        return mean_color_embedding(crop_bgr)


class ColorBodyEmbedder:
    model = "fake-body-4"

    def embed(self, crop_bgr: np.ndarray) -> np.ndarray:
        return mean_color_embedding(crop_bgr)


def mean_color_embedding(crop_bgr: np.ndarray) -> np.ndarray:
    if crop_bgr.size == 0:
        return np.zeros(0, dtype=np.float32)
    mean = crop_bgr.reshape(-1, crop_bgr.shape[-1]).mean(axis=0).astype(np.float32) / 255.0
    return _unit(np.array([*mean.tolist(), 0.05], dtype=np.float32))


def fake_backends(
    frames: list[list[Detection]] | None = None,
    *,
    with_face: bool = True,
    with_body: bool = True,
) -> Backends:
    """A Backends with no model files."""
    return Backends(
        detector=FixedDetector(frames),
        locator=CenterFaceLocator() if with_face else None,
        face=ColorFaceEmbedder() if with_face else None,
        body=ColorBodyEmbedder() if with_body else None,
        health=HealthDict(
            camera=str(HealthState.OK),
            native=str(HealthState.UNAVAILABLE),
            face_model=str(HealthState.OK if with_face else HealthState.NONE),
            body_model=str(HealthState.OK if with_body else HealthState.NONE),
            gpu=str(HealthState.NONE),
        ),
    )


def _unit(vector: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vector))
    return vector if norm < 1e-9 else (vector / norm).astype(np.float32)
