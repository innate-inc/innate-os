"""Host-side basis maintenance: rebuild basis.json's ``safe`` table against the MuJoCo collision model
and report how clean the basis is.

    uv run mars-express basis            # rebuild the table in place, print the validation
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import _core  # noqa: F401
import numpy as np

from brain_client.expressive.basis import ARM_CHANNELS, BASIS_PATH, BODY_CHANNELS, Basis
from brain_client.expressive.channels import NEUTRAL, Ch
from brain_client.expressive.reach import Reach

GRID = tuple(float(g) for g in np.linspace(-1.0, 1.0, 7))
SCAN_STEP = 0.05
MARGIN = 0.05  # back off from the first contact: the table is interpolated between grid points


def _row(weights: tuple[float, ...], scale: float, grip: float) -> np.ndarray:
    row = NEUTRAL.copy()
    row[: len(BODY_CHANNELS)] = weights
    row[list(ARM_CHANNELS)] *= scale
    row[Ch.GRIP] = grip
    return row


def _clear(raw: Basis, reach: Reach, weights: tuple[float, ...], scale: float) -> bool:
    return not any(reach.collides(raw.synthesize(_row(weights, scale, g)).vector) for g in (0.0, 1.0))


def _safe_scale(raw: Basis, reach: Reach, weights: tuple[float, ...]) -> float:
    """``_first_contact``, backed off further until the node itself is clear (contact along a ray is not
    always monotonic: grazing pairs come and go)."""
    scale = round(_first_contact(raw, reach, weights), 3)
    while scale > 0.0 and not _clear(raw, reach, weights, scale):
        scale = round(max(0.0, scale - 0.02), 3)
    return scale


def _first_contact(raw: Basis, reach: Reach, weights: tuple[float, ...]) -> float:
    """Largest scale in [0, 1] reachable from NEUTRAL along the ray to ``weights`` without contact."""
    for grip in (0.0, 1.0):
        if reach.collides(raw.synthesize(_row(weights, 0.0, grip)).vector):
            return 0.0
    safe = 0.0
    for scale in np.arange(SCAN_STEP, 1.0 + 1e-9, SCAN_STEP):
        if any(reach.collides(raw.synthesize(_row(weights, float(scale), g)).vector) for g in (0.0, 1.0)):
            lo, hi = safe, float(scale)
            for _ in range(6):
                mid = 0.5 * (lo + hi)
                if any(reach.collides(raw.synthesize(_row(weights, mid, g)).vector) for g in (0.0, 1.0)):
                    hi = mid
                else:
                    lo = mid
            return max(0.0, lo - MARGIN)
        safe = float(scale)
    return 1.0


def build_safe(data: dict[str, Any]) -> dict[str, Any]:
    raw = Basis({k: v for k, v in data.items() if k != "safe"})
    reach = Reach(raw)
    if not reach.checks_collisions:
        raise RuntimeError("building the safe table needs mujoco and mars.urdf")
    scale = [_safe_scale(raw, reach, w) for w in itertools.product(GRID, repeat=len(BODY_CHANNELS))]
    return {"grid": list(GRID), "scale": scale}


def validate(basis: Basis, samples: int = 3000, seed: int = 0) -> dict[str, Any]:
    """Collision rate of synthesized poses: every -1/0/+1 combination and random rows."""
    reach = Reach(basis)
    rng = np.random.default_rng(seed)
    grid_hits = sum(
        reach.collides(basis.synthesize(_row(w, 1.0, g)).vector)
        for w in itertools.product((-1.0, 0.0, 1.0), repeat=len(BODY_CHANNELS))
        for g in (0.0, 1.0)
    )
    worst = 0.0
    random_hits = 0
    for _ in range(samples):
        row = _row(tuple(rng.uniform(-1, 1, len(BODY_CHANNELS))), 1.0, float(rng.uniform(0, 1)))
        contacts = reach.contacts(basis.synthesize(row).vector)
        random_hits += bool(contacts)
        worst = min([worst, *(d for _a, _b, d in contacts)])
    single = [
        (int(ch), sign)
        for ch in range(len(BODY_CHANNELS))
        for sign in (-1.0, 1.0)
        if reach.collides(basis.synthesize(_row(tuple(sign if i == ch else 0.0 for i in range(5)), 1.0, 1.0)).vector)
    ]
    factors = [basis.safe_factor(_row(tuple(rng.uniform(-1, 1, 5)), 1.0, 0.15)) for _ in range(500)]
    return {
        "grid_collisions": f"{grid_hits}/486",
        "random_collisions": f"{random_hits}/{samples}",
        "worst_penetration_mm": round(-1000 * worst, 1),
        "colliding_single_extremes": single,
        "mean_safe_factor_random": round(float(np.mean(factors)), 3),
    }


def dumps(data: dict[str, Any]) -> str:
    """basis.json text: indented, with the 16807-entry safe table on one line."""
    head = json.dumps({k: v for k, v in data.items() if k != "safe"}, indent=2)
    if "safe" not in data:
        return head + "\n"
    safe = data["safe"]
    table = f'{{"grid": {json.dumps(safe["grid"])}, "scale": {json.dumps(safe["scale"], separators=(",", ":"))}}}'
    return f'{head[:-2]},\n  "safe": {table}\n}}\n'


def rebuild(path: Path = BASIS_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text())
    data["safe"] = build_safe(data)
    path.write_text(dumps(data))
    return validate(Basis(data))
