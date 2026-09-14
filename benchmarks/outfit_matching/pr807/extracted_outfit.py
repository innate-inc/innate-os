# SPDX-License-Identifier: Apache-2.0
# Exact outfit encoder and generic matcher definitions from PR #807, commit
# 487978f9fc69bda93b349423076c3455248ae1c2. Evaluated on faceless mannequin garments only.
# Face identification, enrollment, and person detection are excluded.
from __future__ import annotations

from importlib import import_module

from pathlib import Path

from typing import Any

import cv2

import numpy as np

Box = tuple[float, float, float, float]

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

def _unit(vector: np.ndarray) -> np.ndarray | None:
    norm = float(np.linalg.norm(vector))
    return None if norm < 1e-9 else (vector / norm).astype(np.float32)

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
