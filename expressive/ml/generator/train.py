"""Train the plan -> motion generator on retargeted Reachy motion.

  python -m ml.generator.train --out runs/generator/generator.pt [--steps 20000] [--real-frac 0.5]

Loss = masked flow-matching MSE + a velocity loss on the implied clean estimate x0_hat = x_t - t v (frame-to-frame
differences, weighted by 1 - t); without it fast motion comes out ~2x too slow. Plan dropout (10%) trains the
unconditional branch for classifier-free guidance. Each batch mixes real clips (``--real-frac``) with the synthetic
library; the checkpoint with the best loss on the 12 held-out real emotions is kept.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import numpy as np
import torch

from .data import Bucket, bucketize, build_corpus, fit_stats, split
from .model import MotionGenerator, device


def loss_fn(
    net: MotionGenerator,
    x0: torch.Tensor,
    q: torch.Tensor,
    mask: torch.Tensor,
    plan_drop: float = 0.1,
    vel_w: float = 1.0,
) -> torch.Tensor:
    b, dev = x0.shape[0], x0.device
    has = (torch.rand(b, 1, 1, device=dev) >= plan_drop).float()
    t = torch.sigmoid(torch.randn(b, device=dev) - 0.4)
    t_ = t.view(-1, 1, 1)
    x1 = torch.randn_like(x0)
    xt = t_ * x1 + (1 - t_) * x0
    v = net(xt, t, q, has, mask == 0)
    w = mask.unsqueeze(-1)
    loss = (((v - (x1 - x0)) ** 2) * w).sum() / (w.sum() * x0.shape[-1])
    if vel_w == 0:
        return loss
    x0_hat = xt - t_ * v
    dh, dd = x0_hat[:, 1:] - x0_hat[:, :-1], x0[:, 1:] - x0[:, :-1]
    wv = (mask[:, 1:] * mask[:, :-1]).unsqueeze(-1) * (1 - t_)
    return loss + vel_w * ((dh - dd) ** 2 * wv).sum() / ((dd**2 * wv).sum() + 1e-6)


def _draw(buckets: list[Bucket], probs: np.ndarray, n: int, rng: np.random.Generator) -> Bucket:
    b = buckets[rng.choice(len(buckets), p=probs)]
    idx = torch.as_tensor(rng.integers(0, len(b["X"]), n), device=b["X"].device)
    return {k: v[idx] for k, v in b.items()}


def _concat(a: Bucket, b: Bucket) -> Bucket:
    """Join two batches of different bucket lengths (the shorter is padded and masked)."""
    length = max(a["X"].shape[1], b["X"].shape[1])

    def padded(x: torch.Tensor) -> torch.Tensor:
        extra = length - x.shape[1]
        return torch.nn.functional.pad(x, (0, 0, 0, extra) if x.dim() == 3 else (0, extra))

    return {k: torch.cat([padded(a[k]), padded(b[k])]) for k in a}


def _mixed(
    real: list[Bucket],
    synthetic: list[Bucket],
    probs: tuple[np.ndarray, np.ndarray],
    n_real: int,
    bs: int,
    rng: np.random.Generator,
) -> Bucket:
    if n_real == 0:
        return _draw(synthetic, probs[1], bs, rng)
    batch = _draw(real, probs[0], n_real, rng)
    return batch if n_real == bs else _concat(batch, _draw(synthetic, probs[1], bs - n_real, rng))


def _probs(buckets: list[Bucket]) -> np.ndarray:
    counts = np.array([len(b["X"]) for b in buckets], float)
    return counts / counts.sum()


def train(out: Path, cache: Path, steps: int, bs: int, lr: float, real_frac: float, eval_every: int, seed: int) -> None:
    dev = device()
    torch.manual_seed(seed)
    rng = np.random.default_rng(seed)
    real, held, synthetic = split(build_corpus(cache))
    stats = fit_stats(real + synthetic)
    real_b, syn_b, val_b = bucketize(real, stats, dev), bucketize(synthetic, stats, dev), bucketize(held, stats, dev)
    probs = (_probs(real_b), _probs(syn_b))
    n_real = round(bs * real_frac)
    net = MotionGenerator().to(dev)
    print(
        f"[generator] {len(real)} real + {len(synthetic)} synthetic samples | {len(held)} held-out | "
        f"{sum(p.numel() for p in net.parameters()) / 1e6:.1f}M params | {dev}",
        flush=True,
    )
    opt = torch.optim.AdamW(net.parameters(), lr=lr, weight_decay=0.01)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, lr, total_steps=steps, pct_start=0.05)
    out.parent.mkdir(parents=True, exist_ok=True)
    best, history, start = (float("inf"), 0), [], time.time()
    for step in range(1, steps + 1):
        batch = _mixed(real_b, syn_b, probs, n_real, bs, rng)
        loss = loss_fn(net, batch["X"], batch["Q"], batch["M"])
        opt.zero_grad(set_to_none=True)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0)
        opt.step()
        sched.step()
        history.append(loss.item())
        if step % eval_every and step != steps:
            continue
        net.eval()
        torch.manual_seed(0)
        with torch.no_grad():
            val = float(
                np.mean(
                    [
                        loss_fn(net, v["X"], v["Q"], v["M"], plan_drop=0.0, vel_w=0.0).item()
                        for v in val_b
                        for _ in range(4)
                    ]
                )
            )
        net.train()
        if val < best[0]:
            best = (val, step)
            torch.save(
                {
                    "sd": net.state_dict(),
                    "stats": stats.as_dict(),
                    "step": step,
                    "held_out_loss": val,
                    "real_frac": real_frac,
                },
                out,
            )
        print(
            f"[generator] step {step:5d}/{steps} train {np.mean(history[-eval_every:]):.4f} held-out {val:.4f} "
            f"{(time.time() - start) / step:.3f}s/it",
            flush=True,
        )
    print(f"[generator] kept step {best[1]} (held-out {best[0]:.4f}) -> {out}")


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ml.generator.train", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--out", type=Path, default=Path("runs/generator/generator.pt"))
    ap.add_argument("--cache", type=Path, default=Path("data/corpus.pt"), help="retargeted corpus (rebuilt if absent)")
    ap.add_argument("--steps", type=int, default=20000)
    ap.add_argument("--bs", type=int, default=32)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--real-frac", type=float, default=0.5, help="share of each batch drawn from real clips")
    ap.add_argument("--eval-every", type=int, default=250)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    train(a.out, a.cache, a.steps, a.bs, a.lr, a.real_frac, a.eval_every, a.seed)


if __name__ == "__main__":
    main()
