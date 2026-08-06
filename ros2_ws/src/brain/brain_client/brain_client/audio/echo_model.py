# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Loader for the learned echo predictor used by barge-in detection.

The model (trained by scripts/audio/train_echo_model.py from robot-recorded
TTS/mic pairs) maps a context window of reference log-mel frames to the mic's
expected echo log-mel frame. It replaces the adaptive per-band gains, which
cannot capture the speaker's nonlinearity at high levels.

Inference is a hand-rolled numpy forward pass of the training script's EchoNet
(conv5-relu-conv5-relu-linear, last time step) from an .npz weight file —
torch-CPU took ~8 ms per 10 ms audio frame on the Orin and torch import
alone costs seconds in the input manager, so torch never loads on the robot.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import numpy as np

DEFAULT_CONTEXT_FRAMES = 21  # pre-ctx-field weight files; new files carry their own


def _make_conv1d_same(w: np.ndarray, b: np.ndarray):
    """Compile a conv1d ('same' padding) into a single GEMM: x (C_in,T) -> (C_out,T)."""
    c_out, c_in, k = w.shape
    w2d = np.ascontiguousarray(w.transpose(0, 2, 1).reshape(c_out, k * c_in))
    bcol = b[:, None].astype(np.float32)

    def conv(x: np.ndarray) -> np.ndarray:
        t = x.shape[1]
        xp = np.pad(x, ((0, 0), (k // 2, k // 2)))
        cols = np.empty((k * c_in, t), dtype=np.float32)
        for i in range(k):
            cols[i * c_in : (i + 1) * c_in] = xp[:, i : i + t]
        return w2d @ cols + bcol

    return conv


def load_predictor(path: str, logger=None) -> Callable[[np.ndarray], np.ndarray] | None:
    """Return a (ctx_frames, N_MELS) band-power -> (N_MELS,) predictor, or None."""
    if not path or not os.path.isfile(path):
        return None
    try:
        weights = np.load(path)
        conv1 = _make_conv1d_same(weights["w1"], weights["b1"])
        conv2 = _make_conv1d_same(weights["w2"], weights["b2"])
        wh, bh = weights["wh"].astype(np.float32), weights["bh"].astype(np.float32)
        # Only the last output frame is used, and with two 'same'-padded convs
        # it depends on just (k1//2 + k2//2 + 1) input frames — feeding the
        # full trained window computes ~60 discarded positions per call. The
        # trimmed result is bit-identical (verified against the full context).
        n_ctx = weights["w1"].shape[2] // 2 + weights["w2"].shape[2] // 2 + 1
    except Exception as e:
        if logger:
            logger.error(f"failed to load barge-in echo model {path}: {e}")
        return None

    def predict(ref_ctx: np.ndarray) -> np.ndarray:
        # pad/trim to the receptive field, log-compress like training
        ctx = ref_ctx[-n_ctx:]
        if len(ctx) < n_ctx:
            ctx = np.concatenate([np.tile(ctx[:1], (n_ctx - len(ctx), 1)), ctx])
        x = np.log10(ctx.astype(np.float32) + 1.0).T  # (N_MELS, T)
        h = np.maximum(conv1(x), 0.0)
        h = np.maximum(conv2(h), 0.0)
        y = wh @ h[:, -1] + bh
        return np.maximum(10.0**y - 1.0, 0.0)

    predict.context_frames = n_ctx  # frames the caller needs to supply
    if logger:
        logger.info(f"🧠 barge-in learned echo model loaded: {path} (receptive field {n_ctx} frames)")
    return predict
