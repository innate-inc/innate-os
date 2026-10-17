"""Generator check on the 12 held-out real emotions (retargeted to MARS), from their TRUE plans.

  python -m ml.generator.evaluate --ckpt runs/generator/generator.pt [--seeds 3] [--steps 8]
  python -m ml.generator.evaluate --ckpt ... --bench          # 6 s clip latency, CPU and GPU

identification  is each generated motion closest to its own real clip among the 12? (chance 8.3%, mean rank 6.5);
                distance = time-shift-tolerant RMS over resampled, z-scored 8-channel trajectories
speed           head-servo (deg/s) and fastest arm joint (deg/s) speed percentiles through the core basis: generated vs
                real vs the plan played directly vs the procedural liveliness baseline (too slow = sluggish, too fast =
                jittery)
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import torch

from brain_client.expressive.basis import Act, Basis
from brain_client.expressive.channels import FPS, Ch, Frames
from brain_client.expressive.liveliness import animate
from brain_client.expressive.plan import extract, frames, lowpass

from ..retarget import EMOTIONS, HELD_OUT, Mapping, pollen, pollen_captions, retarget
from .sample import Generator

SAMPLES = 64
BASIS = Basis.load()


class HeldOut:
    """``rank(motion, name)``: where clip ``name`` ranks among the 12 held-out emotions by distance to ``motion``
    (1 = identified); ``rank_all`` ranks it among all 85 real emotions (chance 1.2%), which the 12-way test saturates."""

    def __init__(self, mapping: Mapping | None = None) -> None:
        mapping = mapping or Mapping.load()
        lib = {n: retarget(t, mapping) for n, t in pollen(EMOTIONS).items()}
        every = np.concatenate(list(lib.values()))
        self.mu, self.sd = every.mean(0), every.std(0) + 1e-6
        self.real = {h: lib[h] for h in HELD_OUT}
        self.prompts = {h: pollen_captions(EMOTIONS)[h] for h in HELD_OUT}
        self.feats = {h: self.feat(m) for h, m in self.real.items()}
        self.all_feats = {h: self.feat(m) for h, m in lib.items()}

    def feat(self, motion: Frames) -> Frames:
        u, v = np.linspace(0, 1, SAMPLES), np.linspace(0, 1, len(motion))
        return (np.stack([np.interp(u, v, motion[:, j]) for j in range(motion.shape[1])], -1) - self.mu) / self.sd

    @staticmethod
    def dist(a: Frames, b: Frames, shift: int = 4) -> float:
        n = SAMPLES
        return min(
            float(np.sqrt(((a[max(0, s) : n + min(0, s)] - b[max(0, -s) : n - max(0, s)]) ** 2).mean()))
            for s in range(-shift, shift + 1)
        )

    def error(self, motion: Frames, name: str, shift: int = 4) -> float:
        """Full-rate z-scored RMS to the real clip (same length), tolerant to a +-``shift`` frame offset."""
        a, b = (motion - self.mu) / self.sd, (self.real[name] - self.mu) / self.sd
        n = min(len(a), len(b))
        return min(
            float(np.sqrt(((a[max(0, s) : n + min(0, s)] - b[max(0, -s) : n - max(0, s)]) ** 2).mean()))
            for s in range(-shift, shift + 1)
        )

    def rank(self, motion: Frames, name: str, among_all: bool = False) -> int:
        f = self.feat(motion)
        d = {k: self.dist(f, v) for k, v in (self.all_feats if among_all else self.feats).items()}
        return sorted(d, key=d.__getitem__).index(name) + 1


BANDS_HZ = ((1.0, 2.0), (2.0, 4.0), (4.0, 8.0))


def detail_spectrum(motion: Frames) -> Frames:
    """log10 power of the >1 Hz detail per channel in each band of ``BANDS_HZ`` -> (bands, channels)."""
    detail = motion - lowpass(motion, 1.0)
    power = np.abs(np.fft.rfft(detail, axis=0)) ** 2 / len(motion)
    freqs = np.fft.rfftfreq(len(motion), 1 / FPS)
    return np.stack([np.log10(power[(freqs >= lo) & (freqs < hi)].sum(0) + 1e-9) for lo, hi in BANDS_HZ])


@dataclass(frozen=True)
class Speeds:
    head_p95: float
    head_peak: float
    arm_p95: float
    arm_peak: float


def speeds(motion: Frames) -> Speeds:
    q = BASIS.synthesize_frames(motion)
    head = np.abs(np.diff(q[:, Act.HEAD_DEG])) * FPS
    arm = np.degrees(np.abs(np.diff(q[:, : Act.J6], axis=0)).max(1)) * FPS
    return Speeds(float(np.percentile(head, 95)), float(head.max()), float(np.percentile(arm, 95)), float(arm.max()))


def _mean(xs: list[Speeds]) -> Speeds:
    return Speeds(*np.mean([[s.head_p95, s.head_peak, s.arm_p95, s.arm_peak] for s in xs], 0).tolist())


def identification(gen: Generator, seeds: int, steps: int) -> dict[str, dict[str, float]]:
    """Top-1 / mean rank and speeds for: the generator, the true plan played directly, and the procedural baseline."""
    held = HeldOut()
    ranks: dict[str, list[int]] = {"generator": [], "plan direct": [], "procedural": []}
    ranks_all: dict[str, list[int]] = {k: [] for k in ranks}
    errors: dict[str, list[float]] = {k: [] for k in ranks}
    spectra: dict[str, list[float]] = {k: [] for k in ranks}
    speed: dict[str, list[Speeds]] = {"real": [], "generator": [], "plan direct": [], "procedural": []}
    for name, real in held.real.items():
        plan = extract(real)
        conditioning = frames(plan, len(real))
        candidates = {
            "plan direct": [conditioning[:, : Ch.ENERGY]],
            "procedural": [animate(conditioning, seed) for seed in range(seeds)],
            "generator": gen.generate_batch([plan] * seeds, seeds=list(range(seeds)), steps=steps),
        }
        speed["real"].append(speeds(real))
        for label, motions in candidates.items():
            ranks[label] += [held.rank(m, name) for m in motions]
            ranks_all[label] += [held.rank(m, name, among_all=True) for m in motions]
            errors[label] += [held.error(m, name) for m in motions]
            spectra[label] += [float(np.abs(detail_spectrum(m) - detail_spectrum(real))[:, :6].mean()) for m in motions]
            speed[label] += [speeds(m) for m in motions]
    report: dict[str, dict[str, float]] = {label: asdict(_mean(xs)) for label, xs in speed.items()}
    for label, r in ranks.items():
        report[label] |= {
            "top1": float(np.mean(np.array(r) == 1)),
            "mean_rank": float(np.mean(r)),
            "top1_of_85": float(np.mean(np.array(ranks_all[label]) == 1)),
            "mean_rank_of_85": float(np.mean(ranks_all[label])),
            "rms_to_real": float(np.mean(errors[label])),
            "spectrum_err": float(np.mean(spectra[label])),
        }
    for label, row in report.items():
        ident = (
            (
                f"top-1 {100 * row['top1']:3.0f}% rank {row['mean_rank']:4.2f} | of 85: top-1 {100 * row['top1_of_85']:3.0f}% "
                f"rank {row['mean_rank_of_85']:5.2f} | rms {row['rms_to_real']:4.2f} | spec {row['spectrum_err']:4.2f}"
            )
            if label in ranks
            else " " * 73
        )
        print(
            f"  {label:12s} {ident}   head p95 / peak {row['head_p95']:5.1f} / {row['head_peak']:5.1f} deg/s   "
            f"arm joint p95 / peak {row['arm_p95']:5.1f} / {row['arm_peak']:5.1f} deg/s"
        )
    print(
        f"  (identification among the 12 held-out real emotions / all 85, from their true plans; chance 8.3% / 6.5; "
        f"n={len(ranks['generator'])}; rms = z-scored distance to the real clip; spec = mean |log10 band power| error "
        f"of the >1 Hz detail vs the real clip, body + base channels)"
    )
    return report


def bench(ckpt: Path, steps: int, seconds: float = 6.0, repeats: int = 20) -> None:
    """Latency of one ``seconds`` clip (CFG doubles the batch) on CPU (4 threads and all) and GPU."""
    plan = extract(np.zeros((round(seconds * FPS) + 1, Ch.ENERGY)))
    targets = [("cpu", 4), ("cpu", torch.get_num_threads())] + ([("cuda", 0)] if torch.cuda.is_available() else [])
    for dev, threads in targets:
        if threads:
            torch.set_num_threads(threads)
        gen = Generator(ckpt, dev)
        gen.generate_batch([plan], steps=steps)
        times = []
        for i in range(repeats):
            start = time.perf_counter()
            gen.generate_batch([plan], seeds=[i], steps=steps)
            if dev == "cuda":
                torch.cuda.synchronize()
            times.append(time.perf_counter() - start)
        label = f"{dev} x{threads}" if threads else dev
        print(
            f"  {label:9s} {seconds:.0f} s clip, {steps} steps: median {1e3 * np.median(times):6.1f} ms  p90 {1e3 * np.percentile(times, 90):6.1f} ms"
        )


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ml.generator.evaluate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--ckpt", type=Path, default=Path("runs/generator/generator.pt"))
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--steps", type=int, default=8)
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--out", type=Path, help="write the report as JSON")
    a = ap.parse_args()
    if a.bench:
        bench(a.ckpt, a.steps)
        return
    report = identification(Generator(a.ckpt), a.seeds, a.steps)
    if a.out:
        a.out.write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
