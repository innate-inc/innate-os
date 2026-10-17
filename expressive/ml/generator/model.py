"""The motion generator: a small flow-matching transformer (21.8M parameters by default; Binh Pham's design).

Input per frame: the noisy 8-channel motion x_t (z-scored), the 9 plan channels interpolated to that frame (z-scored)
times a ``has_plan`` flag, and the flag itself. The plan is concatenated to every frame rather than cross-attended,
so the model cannot ignore it. The flow time enters through AdaLN (zero-initialised, so each block starts as the
identity). The output is the velocity v = x1 - x0 (noise minus data).
"""

from __future__ import annotations

import math

import torch
from torch import nn

N_DOF, N_PLAN = 8, 9
MAXLEN = 720  # frames: 28.8 s at 25 Hz


def device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def timestep_embedding(t: torch.Tensor, dim: int = 256) -> torch.Tensor:
    half = dim // 2
    freqs = torch.exp(-math.log(10000) * torch.arange(half, device=t.device) / half)
    angles = t[:, None] * freqs[None] * 1000.0
    return torch.cat([angles.sin(), angles.cos()], -1)


class Block(nn.Module):
    def __init__(self, d: int, heads: int) -> None:
        super().__init__()
        self.n1 = nn.LayerNorm(d, elementwise_affine=False)
        self.n2 = nn.LayerNorm(d, elementwise_affine=False)
        self.att = nn.MultiheadAttention(d, heads, batch_first=True)
        self.mlp = nn.Sequential(nn.Linear(d, 4 * d), nn.GELU(), nn.Linear(4 * d, d))
        modulation = nn.Linear(d, 6 * d)
        nn.init.zeros_(modulation.weight)
        nn.init.zeros_(modulation.bias)
        self.ada = nn.Sequential(nn.SiLU(), modulation)

    def forward(self, x: torch.Tensor, c: torch.Tensor, pad: torch.Tensor) -> torch.Tensor:
        a1, b1, g1, a2, b2, g2 = self.ada(c).unsqueeze(1).chunk(6, -1)
        h = self.n1(x) * (1 + b1) + a1
        x = x + g1 * self.att(h, h, h, key_padding_mask=pad, need_weights=False)[0]
        h = self.n2(x) * (1 + b2) + a2
        return x + g2 * self.mlp(h)


class MotionGenerator(nn.Module):
    def __init__(self, d: int = 384, heads: int = 6, layers: int = 8, maxlen: int = MAXLEN) -> None:
        super().__init__()
        self.maxlen = maxlen
        self.inp = nn.Linear(N_DOF + N_PLAN + 1, d)
        self.pos = nn.Parameter(torch.randn(1, maxlen, d) * 0.02)
        self.temb = nn.Sequential(nn.Linear(256, d), nn.SiLU(), nn.Linear(d, d))
        self.blocks = nn.ModuleList([Block(d, heads) for _ in range(layers)])
        self.nf = nn.LayerNorm(d)
        self.out = nn.Linear(d, N_DOF)
        nn.init.zeros_(self.out.weight)
        nn.init.zeros_(self.out.bias)

    def forward(
        self, x: torch.Tensor, t: torch.Tensor, plan: torch.Tensor, has: torch.Tensor, pad: torch.Tensor
    ) -> torch.Tensor:
        """x (B,T,8) noisy motion; t (B,) flow time (1 = noise); plan (B,T,9); has (B,1,1); pad (B,T) True = padding."""
        cond = torch.cat([plan * has, has.expand(-1, x.shape[1], 1)], -1)
        h = self.inp(torch.cat([x, cond], -1)) + self.pos[:, : x.shape[1]]
        c = self.temb(timestep_embedding(t))
        for block in self.blocks:
            h = block(h, c, pad)
        return self.out(self.nf(h))
