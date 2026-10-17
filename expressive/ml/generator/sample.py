"""Plans -> 25 Hz MARS plan-space motion with the trained generator.

  gen = Generator("runs/generator/generator.pt")
  motions = gen.generate_batch(plans)              # list of (T, 8), clipped to the channel ranges
  generate(plan.frames(plan), seed)                # the eval harness's arm: --arm flow=ml.generator.sample:generate

Euler integration of the flow from noise (t = 1) to data (t = 0) with classifier-free guidance on the plan, then a
4 Hz zero-phase low-pass (the core's numpy Butterworth, so the robot could run this without scipy). All plans
(padded to the longest, masked) and both guidance branches share one forward per step.
"""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import torch

from brain_client.expressive.channels import Frames, clip_to_limits
from brain_client.expressive.liveliness import animate
from brain_client.expressive.plan import frames, lowpass

from .model import N_DOF, N_PLAN, MotionGenerator, device

if TYPE_CHECKING:
    from collections.abc import Sequence

    from brain_client.expressive.plan import Plan

STEPS = 8
CFG = 1.5
LOWPASS_HZ = 4.0
# Where generate() finds the checkpoint (gitignored; copy of the 5090's runs/generator/generator.pt).
CHECKPOINT = Path(os.environ.get("MARS_FLOW_GENERATOR", Path(__file__).parents[2] / "out" / "models" / "generator.pt"))


class Generator:
    def __init__(self, ckpt: Path | str, dev: str | None = None) -> None:
        self.dev = dev or device()
        blob = torch.load(ckpt, map_location=self.dev, weights_only=False)
        self.net = MotionGenerator().to(self.dev).eval()
        self.net.load_state_dict(blob["sd"])
        stats = blob["stats"]
        self.mu, self.sd = np.array(stats["mu"]), np.array(stats["sd"])
        self.pmu, self.psd = np.array(stats["pmu"]), np.array(stats["psd"])
        self.step = int(blob.get("step", 0))

    def generate_frames(
        self,
        plan_frames: Sequence[Frames],
        seeds: Sequence[int] | None = None,
        steps: int = STEPS,
        cfg: float = CFG,
        lowpass_hz: float | None = LOWPASS_HZ,
    ) -> list[Frames]:
        """(T_i, 9) interpolated plans -> (T_i, 8) motions. A plan longer than the model's 28.8 s window (a 30 s
        recipe slowed by tempo jitter) is animated by the core's procedural liveliness instead of being truncated."""
        seeds = list(seeds) if seeds is not None else list(range(len(plan_frames)))
        fits = [i for i, p in enumerate(plan_frames) if len(p) <= self.net.maxlen]
        out = {i: animate(p, seeds[i]) for i, p in enumerate(plan_frames) if len(p) > self.net.maxlen}
        if fits:
            sampled = self._sample([plan_frames[i] for i in fits], [seeds[i] for i in fits], steps, cfg, lowpass_hz)
            out.update(zip(fits, sampled, strict=True))
        return [out[i] for i in range(len(plan_frames))]

    @torch.no_grad()
    def _sample(
        self, plans: Sequence[Frames], seeds: Sequence[int], steps: int, cfg: float, lowpass_hz: float | None
    ) -> list[Frames]:
        lengths = [len(p) for p in plans]
        b, length = len(plans), max(lengths)
        q = torch.zeros(b, length, N_PLAN, device=self.dev)
        pad = torch.ones(b, length, dtype=torch.bool, device=self.dev)
        x = torch.zeros(b, length, N_DOF, device=self.dev)
        for i, p in enumerate(plans):
            q[i, : lengths[i]] = torch.tensor((p - self.pmu) / self.psd, dtype=torch.float32, device=self.dev)
            pad[i, : lengths[i]] = False
            g = torch.Generator(device="cpu").manual_seed(int(seeds[i]))
            x[i, : lengths[i]] = torch.randn(lengths[i], N_DOF, generator=g).to(self.dev)
        guided = cfg != 1.0
        has = torch.ones(b, 1, 1, device=self.dev)
        if guided:
            has, q, pad = torch.cat([has, torch.zeros_like(has)]), torch.cat([q, q]), torch.cat([pad, pad])
        for k in range(steps):
            t = torch.full((len(has),), 1.0 - k / steps, device=self.dev)
            v = self.net(torch.cat([x, x]) if guided else x, t, q, has, pad)
            if guided:
                v = v[b:] + cfg * (v[:b] - v[b:])
            x = x - v / steps
        out: list[Frames] = []
        for i in range(b):
            motion = x[i, : lengths[i]].cpu().double().numpy() * self.sd + self.mu
            if lowpass_hz:
                motion = lowpass(motion, lowpass_hz)
            out.append(clip_to_limits(motion))
        return out

    def generate_batch(
        self,
        plans: Sequence[Plan],
        seeds: Sequence[int] | None = None,
        steps: int = STEPS,
        cfg: float = CFG,
        lowpass_hz: float | None = LOWPASS_HZ,
    ) -> list[Frames]:
        """Plans -> (T_i, 8) motions."""
        return self.generate_frames([frames(p) for p in plans], seeds, steps, cfg, lowpass_hz)


@cache
def _default() -> Generator:
    return Generator(CHECKPOINT)


def generate(plan_frames: Frames, seed: int) -> Frames:
    """(T, 9) plan frames (core ``plan.frames``) -> (T, 8) motion; loads ``CHECKPOINT`` on first use (CUDA, MPS or CPU)."""
    return _default().generate_frames([plan_frames], [seed])[0]
