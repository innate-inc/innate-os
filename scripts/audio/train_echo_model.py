#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Train the barge-in echo predictor from robot-recorded TTS/mic pairs.

Input: a directory produced by scripts/audio/collect_echo_pairs.py
(<NNNN>_ref.raw 44.1k s16 mono, <NNNN>_mic.raw 24k s16 mono, <NNNN>_meta.json).

The model maps a context window of reference log-mel frames to the mic's
observed log-mel frame — i.e. it learns the speaker+room+mic transfer
including the speaker's nonlinearity, which the runtime's linear per-band
gains cannot represent. Runs anywhere with torch (intended for the 5090 box);
exports TorchScript for the robot (loaded by brain_client.audio.echo_model).

    python3 train_echo_model.py --data echo_pairs/ --out echo_model.pt

Feature parameters MUST match brain_client/audio/barge_in.py.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn

# --- feature extraction: keep in sync with brain_client/audio/barge_in.py ---
MIC_RATE = 24_000
FRAME = 512
HOP = 240
N_MELS = 24
FMIN, FMAX = 100.0, 8000.0
# Frames of reference context per training example. NOTE: with two 'same'-padded
# conv layers the predicted (last) frame only ever sees the last 5 input frames
# — widening CONTEXT alone does NOT let the model span the reverb tail (63 was
# tried and scored slightly worse than 21 while costing 3x the compute). To
# actually use longer context the architecture needs dilation or more layers.
CONTEXT = 21


def mel_filterbank():
    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + np.asarray(f) / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10.0 ** (np.asarray(m) / 2595.0) - 1.0)

    mel_pts = np.linspace(hz_to_mel(FMIN), hz_to_mel(FMAX), N_MELS + 2)
    hz_pts = mel_to_hz(mel_pts)
    bins = np.floor((FRAME // 2 + 1) * hz_pts / (MIC_RATE / 2)).astype(int)
    fb = np.zeros((N_MELS, FRAME // 2 + 1), dtype=np.float32)
    for i in range(N_MELS):
        lo, mid, hi = bins[i], bins[i + 1], bins[i + 2]
        if mid > lo:
            fb[i, lo:mid] = np.linspace(0, 1, mid - lo, endpoint=False)
        if hi > mid:
            fb[i, mid:hi] = np.linspace(1, 0, hi - mid, endpoint=False)
    return fb


FB = mel_filterbank()
WINDOW = np.hanning(FRAME).astype(np.float32)


def band_frames(samples: np.ndarray) -> np.ndarray:
    n = (len(samples) - FRAME) // HOP + 1
    if n <= 0:
        return np.zeros((0, N_MELS), dtype=np.float32)
    idx = np.arange(FRAME)[None, :] + HOP * np.arange(n)[:, None]
    frames = samples[idx] * WINDOW
    power = np.abs(np.fft.rfft(frames, axis=1)) ** 2 / FRAME
    return (power @ FB.T).astype(np.float32)


def resample(pcm: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return pcm
    x_old = np.arange(len(pcm)) / src
    x_new = np.arange(int(len(pcm) * dst / src)) / dst
    return np.interp(x_new, x_old, pcm)


def align(ref_b: np.ndarray, mic_b: np.ndarray, max_lag=600) -> int:
    """Frame lag maximizing envelope correlation (mic starts before playback)."""

    def env(b):
        e = np.log10(np.sum(b, axis=1) + 1e-3)
        return e - e.mean()

    re_, me = env(ref_b), env(mic_b)
    best, best_lag = -2.0, 0
    for lag in range(0, min(max_lag, len(me) - 50)):
        n = min(len(re_), len(me) - lag)
        if n < 100:
            break
        c = np.dot(re_[:n], me[lag : lag + n]) / (np.linalg.norm(re_[:n]) * np.linalg.norm(me[lag : lag + n]) + 1e-6)
        if c > best:
            best, best_lag = c, lag
    return best_lag if best > 0.5 else -1


class EchoNet(nn.Module):
    """(B, CONTEXT, N_MELS) ref log-mel -> (B, N_MELS) predicted mic log-mel."""

    def __init__(self, hidden=64):
        super().__init__()
        self.conv = nn.Sequential(
            nn.Conv1d(N_MELS, hidden, kernel_size=5, padding=2),
            nn.ReLU(),
            nn.Conv1d(hidden, hidden, kernel_size=5, padding=2),
            nn.ReLU(),
        )
        self.head = nn.Linear(hidden, N_MELS)

    def forward(self, x):
        h = self.conv(x.transpose(1, 2))  # (B, hidden, T)
        return self.head(h[:, :, -1])


def load_pairs(data_dir: Path):
    xs, ys = [], []
    for meta_path in sorted(data_dir.glob("*_meta.json")):
        stem = meta_path.name.replace("_meta.json", "")
        meta = json.loads(meta_path.read_text())
        ref = np.frombuffer((data_dir / f"{stem}_ref.raw").read_bytes(), dtype=np.int16).astype(np.float32)
        mic = np.frombuffer((data_dir / f"{stem}_mic.raw").read_bytes(), dtype=np.int16).astype(np.float32)
        ref = resample(ref, int(meta.get("ref_rate", 44100)), MIC_RATE)
        ref_b, mic_b = band_frames(ref), band_frames(mic)
        lag = align(ref_b, mic_b)
        if lag < 0:
            print(f"  {stem}: alignment failed, skipped")
            continue
        n = min(len(ref_b), len(mic_b) - lag)
        ref_log = np.log10(ref_b[:n] + 1.0)
        mic_log = np.log10(mic_b[lag : lag + n] + 1.0)
        for t in range(CONTEXT, n):
            xs.append(ref_log[t - CONTEXT : t])
            ys.append(mic_log[t - 1])
        print(f"  {stem}: lag={lag * 10}ms, {n - CONTEXT} samples")
    return np.stack(xs), np.stack(ys)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--val-split", type=float, default=0.1)
    ap.add_argument(
        "--quantile",
        type=float,
        default=0.95,
        help="Pinball-loss quantile. The runtime uses the prediction as a floor, so "
        "under-prediction causes false triggers while over-prediction only costs a "
        "little sensitivity: fit a high quantile, not the mean. The speaker's "
        "level-dependent distortion lives in exactly the tail MSE ignores. "
        "0.5 = plain median (symmetric).",
    )
    args = ap.parse_args()

    print(f"loading pairs from {args.data} ...")
    x, y = load_pairs(args.data)
    print(f"dataset: {len(x)} frames")
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    n_val = max(1, int(len(x) * args.val_split))
    perm = np.random.default_rng(0).permutation(len(x))
    tr, va = perm[n_val:], perm[:n_val]
    xt = torch.from_numpy(x).float()
    yt = torch.from_numpy(y).float()

    model = EchoNet().to(dev)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    q = args.quantile

    def pinball(pred, target):
        e = target - pred
        return torch.mean(torch.maximum(q * e, (q - 1) * e))

    best_val = float("inf")
    for epoch in range(args.epochs):
        model.train()
        np.random.shuffle(tr)
        tot = 0.0
        for i in range(0, len(tr), args.batch):
            idx = tr[i : i + args.batch]
            xb, yb = xt[idx].to(dev), yt[idx].to(dev)
            loss = pinball(model(xb), yb)
            opt.zero_grad()
            loss.backward()
            opt.step()
            tot += loss.item() * len(idx)
        model.eval()
        with torch.no_grad():
            pv = model(xt[va].to(dev))
            val = pinball(pv, yt[va].to(dev)).item()
            under = torch.mean(((yt[va].to(dev) - pv) > 0.3).float()).item()  # >3dB under-predicted
        print(f"epoch {epoch + 1}: train {tot / len(tr):.4f}  val {val:.4f}  frames>3dB-under: {under * 100:.1f}%")
        if val < best_val:
            best_val = val
            scripted = torch.jit.trace(model.cpu().eval(), xt[:2])
            scripted.save(str(args.out))
            save_npz(model, args.out.with_suffix(".npz"))
            model.to(dev)
    print(f"saved best model (val {best_val:.4f}) -> {args.out} (+ .npz for torch-free inference)")


def save_npz(model: EchoNet, path):
    """Weights as npz so the robot runs inference in numpy (no torch import)."""
    sd = {k: v.detach().cpu().numpy() for k, v in model.state_dict().items()}
    np.savez(
        path,
        w1=sd["conv.0.weight"],
        b1=sd["conv.0.bias"],
        w2=sd["conv.2.weight"],
        b2=sd["conv.2.bias"],
        wh=sd["head.weight"],
        bh=sd["head.bias"],
        ctx=np.array(CONTEXT),
    )


if __name__ == "__main__":
    main()
