"""Training pairs: retargeted MARS motion -> its own extracted plan, augmented, z-scored and length-bucketed.

Two pools, mixed per batch by the trainer: Pollen's real clips (emotions + dances, minus the 12 held-out
emotions; x {original, sagittal mirror} x {0.8, 1, 1.25} time-stretch, each variant with its OWN re-extracted
plan) and Binh's synthetic library (one pair per episode). Plans come from the core's ``plan.extract`` (1 Hz
posture, 0.5 s keys, energy), exactly what serving-time plans are drawn against.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import torch

from brain_client.expressive.channels import Ch, Frames
from brain_client.expressive.plan import extract, frames

from ..retarget import DANCES, EMOTIONS, HELD_OUT, Mapping, massive, pollen, retarget
from .model import MAXLEN

if TYPE_CHECKING:
    from collections.abc import Sequence

BUCKETS = (104, 176, 296, 496, MAXLEN)
STRETCH = (0.8, 1.0, 1.25)

Sample = tuple[Frames, Frames]  # (T, 8) motion, (T, 9) per-frame plan
Bucket = dict[str, torch.Tensor]  # X (N, L, 8), Q (N, L, 9), M (N, L)


@dataclass(frozen=True)
class Stats:
    mu: list[float]
    sd: list[float]
    pmu: list[float]
    psd: list[float]

    def as_dict(self) -> dict[str, list[float]]:
        return asdict(self)


@dataclass(frozen=True)
class Corpus:
    real: dict[str, Frames]
    synthetic: list[Frames]


def mirror(motion: Frames) -> Frames:
    """Sagittal mirror: cant and base turn change side; everything else is symmetric."""
    out = motion.copy()
    out[:, [Ch.ASKEW, Ch.ORIENT]] *= -1
    return out


def stretch(motion: Frames, factor: float) -> Frames:
    """Uniform time-stretch (factor > 1 = slower / longer)."""
    count = max(8, round(len(motion) * factor))
    u = np.linspace(0, len(motion) - 1, count)
    grid = np.arange(len(motion))
    return np.stack([np.interp(u, grid, motion[:, j]) for j in range(motion.shape[1])], -1)


def pair(motion: Frames) -> Sample:
    motion = motion[:MAXLEN]
    return motion, frames(extract(motion), len(motion))


def augmented(motion: Frames) -> list[Sample]:
    return [pair(stretch(m, f)) for m in (motion, mirror(motion)) for f in STRETCH]


def build_corpus(cache: Path, mapping: Mapping | None = None) -> Corpus:
    """Retarget every source once; cached as a .pt file (pass a fresh path after retuning retarget.json)."""
    if cache.exists():
        blob = torch.load(cache, weights_only=False)
        return Corpus(real=blob["real"], synthetic=blob["synthetic"])
    mapping = mapping or Mapping.load()
    real = {name: retarget(t, mapping) for name, t in (pollen(EMOTIONS) | pollen(DANCES)).items()}
    synthetic = [retarget(ep.traj, mapping) for ep in massive() if len(ep.traj) >= 16]
    cache.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"real": real, "synthetic": synthetic}, cache)
    return Corpus(real=real, synthetic=synthetic)


def split(corpus: Corpus, held_out: Sequence[str] = HELD_OUT) -> tuple[list[Sample], list[Sample], list[Sample]]:
    """(real train, real held-out, synthetic) samples."""
    train = [s for name, m in corpus.real.items() if name not in held_out for s in augmented(m)]
    val = [s for name, m in corpus.real.items() if name in held_out for s in augmented(m)]
    return train, val, [pair(m) for m in corpus.synthetic]


def fit_stats(samples: Sequence[Sample]) -> Stats:
    motion = np.concatenate([m for m, _ in samples])
    plan = np.concatenate([p for _, p in samples])
    return Stats(
        mu=motion.mean(0).tolist(),
        sd=(motion.std(0) + 1e-6).tolist(),
        pmu=plan.mean(0).tolist(),
        psd=(plan.std(0) + 1e-6).tolist(),
    )


def bucketize(samples: Sequence[Sample], stats: Stats, dev: str) -> list[Bucket]:
    mu, sd, pmu, psd = (np.array(v) for v in (stats.mu, stats.sd, stats.pmu, stats.psd))
    out: list[Bucket] = []
    for lo, hi in zip((0, *BUCKETS[:-1]), BUCKETS, strict=True):
        chosen = [(m, p) for m, p in samples if lo < len(m) <= hi]
        if not chosen:
            continue
        x = np.zeros((len(chosen), hi, len(mu)), np.float32)
        q = np.zeros((len(chosen), hi, len(pmu)), np.float32)
        mask = np.zeros((len(chosen), hi), np.float32)
        for i, (m, p) in enumerate(chosen):
            x[i, : len(m)] = (m - mu) / sd
            q[i, : len(m)] = (p - pmu) / psd
            mask[i, : len(m)] = 1
        out.append(
            {"X": torch.tensor(x, device=dev), "Q": torch.tensor(q, device=dev), "M": torch.tensor(mask, device=dev)}
        )
    return out
