"""Reachy Mini motion -> MARS plan-space motion (CONTRACTS §1) at 25 Hz, so the generator learns real timing.

Sources: Pollen's recorded emotions (85) and dances (19) (move JSON ``{time, set_target_data: [{head 4x4,
antennas [r, l], body_yaw}]}``, resampled to 25 Hz by their own timestamps) and Binh's synthetic 10,872-episode
library (LeRobot v3 parquet, 25 Hz 9-DoF state, prompts and teacher rows in ``meta/``).

The mapping is data (``retarget.json``) so it can be retuned by rendering:

  python -m ml.retarget describe sad1 amazed1 cheerful1     # per-channel summary of retargeted clips
  python -m ml.retarget clips --out DIR [names...]          # Clip JSON (plan space) for the renderer / studio
  python -m ml.retarget plot sad1 amazed1 --out sheet.png   # channel curves
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from brain_client.expressive.channels import FPS, MOTION_KEYS, Ch, Frames, clip_to_limits
from brain_client.expressive.plan import lowpass

if TYPE_CHECKING:
    from collections.abc import Iterator

EMOTIONS = "pollen-robotics/reachy-mini-emotions-library"
DANCES = "pollen-robotics/reachy-mini-dances-library"
MASSIVE = "binhpham/reachy-mini-massive-motion-library"
MAPPING = Path(__file__).with_name("retarget.json")

# Real emotions kept out of generator training for evaluation (Binh's choice: they span the space).
HELD_OUT = (
    "disgusted1",
    "downcast1",
    "electric1",
    "exhausted1",
    "frustrated1",
    "lonely1",
    "rage1",
    "relief1",
    "surprised1",
    "thoughtful1",
    "welcoming1",
    "impatient1",
)

# Reachy 9-DoF trajectory columns: x y z (m) | roll pitch yaw (rad) | antenna_right antenna_left (rad) | body_yaw (rad).
ReachyTraj = Frames
FEATURES = ("x_mm", "y_mm", "z_mm", "roll_deg", "pitch_deg", "yaw_deg", "body_deg")


@dataclass(frozen=True)
class Episode:
    name: str
    prompt: str
    source: str
    family: str
    traj: ReachyTraj


@dataclass(frozen=True)
class Mapping:
    rest: dict[str, float]
    linear: dict[str, dict[str, float]]
    ear_rest: float
    ear_up: float
    ear_down: float
    expand_gain: float
    grip_points: tuple[tuple[float, float], ...]
    flick_hz: float
    flick_deadband: float
    flick_full: float
    flick_gain: float
    detail_hz: float
    detail_gain: tuple[float, ...]

    @classmethod
    def load(cls, path: Path = MAPPING) -> Mapping:
        d = json.loads(path.read_text())
        unknown = {f for row in d["linear"].values() for f in row} - set(FEATURES)
        if unknown or set(d["linear"]) - set(MOTION_KEYS):
            raise ValueError(
                f"retarget.json: unknown features {unknown} or channels {set(d['linear']) - set(MOTION_KEYS)}"
            )
        return cls(
            rest=d["rest"],
            linear=d["linear"],
            ear_rest=d["ears"]["rest_deg"],
            ear_up=d["ears"]["up_deg"],
            ear_down=d["ears"]["down_deg"],
            expand_gain=d["expand_gain"],
            grip_points=tuple((float(u), float(g)) for u, g in d["grip_points"]),
            flick_hz=d["flick_hz"],
            flick_deadband=d["flick_deadband_deg"],
            flick_full=d["flick_full_deg"],
            flick_gain=d["flick_gain"],
            detail_hz=d["detail_hz"],
            detail_gain=tuple(float(d["detail_gain"].get(k, 1.0)) for k in MOTION_KEYS),
        )


def dataset_dir(repo: str) -> Path:
    """Local snapshot of a Hub dataset's motion files (reuses the HF_HOME cache; completes a partial download)."""
    from huggingface_hub import snapshot_download

    return Path(snapshot_download(repo, repo_type="dataset", allow_patterns=["*.json", "*.jsonl", "*.parquet"]))


def _rpy(rot: Frames) -> Frames:
    """Rotation matrices (..., 3, 3) -> extrinsic xyz roll / pitch / yaw (as the Reachy SDK)."""
    sy = np.hypot(rot[..., 0, 0], rot[..., 1, 0])
    return np.stack(
        [
            np.arctan2(rot[..., 2, 1], rot[..., 2, 2]),
            np.arctan2(-rot[..., 2, 0], sy),
            np.arctan2(rot[..., 1, 0], rot[..., 0, 0]),
        ],
        -1,
    )


def move_traj(move: dict[str, Any]) -> ReachyTraj:
    """Move JSON -> (T, 9) at 25 Hz, resampled with the move's own timestamps (raw frames are ~50-100 Hz)."""
    frames = move["set_target_data"]
    t = np.asarray(move["time"], float)
    t -= t[0]
    head = np.array([f["head"] for f in frames], float)
    raw = np.concatenate(
        [
            head[:, :3, 3],
            _rpy(head[:, :3, :3]),
            np.array([f["antennas"] for f in frames], float),
            np.array([[f.get("body_yaw", 0.0)] for f in frames], float),
        ],
        -1,
    )
    grid = np.arange(max(2, round(float(t[-1]) * FPS) + 1)) / FPS
    return np.stack([np.interp(grid, t, raw[:, j]) for j in range(9)], -1)


def pollen(repo: str = EMOTIONS) -> dict[str, ReachyTraj]:
    """Every clip of a Pollen moves library, by name."""
    out: dict[str, ReachyTraj] = {}
    for path in sorted(glob.glob(str(dataset_dir(repo) / "*.json"))):
        move = json.loads(Path(path).read_text())
        if "set_target_data" in move:
            out[os.path.basename(path)[:-5]] = move_traj(move)
    return out


def caption(name: str, description: str) -> str:
    """("downcast1", "...") -> "downcast. <description>" (the prompt a clip stands for)."""
    word = re.sub(r"[-_]?\d+$", "", name).replace("_", " ").replace("-", " ")
    return f"{word}. {description}".strip()


def pollen_captions(repo: str = EMOTIONS) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(glob.glob(str(dataset_dir(repo) / "*.json"))):
        move = json.loads(Path(path).read_text())
        out[os.path.basename(path)[:-5]] = caption(os.path.basename(path)[:-5], move.get("description", ""))
    return out


def massive(repo: str = MASSIVE) -> Iterator[Episode]:
    """Binh's synthetic library: one Episode per row (25 Hz state, teacher prompt/source/family)."""
    import pyarrow.parquet as pq

    root = dataset_dir(repo)
    rows = {
        r["episode_index"]: r for r in map(json.loads, (root / "meta" / "teacher_rows.jsonl").read_text().splitlines())
    }
    for path in sorted(glob.glob(str(root / "data" / "*" / "*.parquet"))):
        table = pq.read_table(path, columns=["observation.state", "episode_index", "frame_index"])
        state = np.stack(table.column("observation.state").to_numpy(zero_copy_only=False)).astype(float)
        episode = table.column("episode_index").to_numpy()
        order = np.lexsort((table.column("frame_index").to_numpy(), episode))
        state, episode = state[order], episode[order]
        bounds = np.flatnonzero(np.diff(episode)) + 1
        for chunk, idx in zip(np.split(state, bounds), np.split(episode, bounds), strict=True):
            row = rows.get(int(idx[0]), {})
            yield Episode(
                name=row.get("id", f"episode-{int(idx[0])}"),
                prompt=row.get("prompt", ""),
                source=row.get("source", ""),
                family=row.get("family", ""),
                traj=chunk,
            )


def features(traj: ReachyTraj, rest: dict[str, float]) -> dict[str, Frames]:
    deg = np.degrees
    raw = {
        "x_mm": 1000 * traj[:, 0],
        "y_mm": 1000 * traj[:, 1],
        "z_mm": 1000 * traj[:, 2],
        "roll_deg": deg(traj[:, 3]),
        "pitch_deg": deg(traj[:, 4]),
        "yaw_deg": deg(traj[:, 5]),
        "body_deg": deg(traj[:, 8]),
    }
    return {k: v - rest.get(k, 0.0) for k, v in raw.items()}


def ear_droop(traj: ReachyTraj) -> Frames:
    """(T, 2) droop in degrees, 0 = straight up (the right antenna droops with negative angles)."""
    return np.stack([-np.degrees(traj[:, 6]), np.degrees(traj[:, 7])], -1)


def retarget(traj: ReachyTraj, mapping: Mapping) -> Frames:
    """(T, 9) Reachy trajectory -> (T, 8) MARS motion in plan units, clipped to the channel ranges."""
    feats = features(traj, mapping.rest)
    out = np.zeros((len(traj), len(MOTION_KEYS)))
    for key, row in mapping.linear.items():
        out[:, MOTION_KEYS.index(key)] = sum(coef * feats[f] for f, coef in row.items())
    ears = ear_droop(traj)
    droop = ears.mean(1)
    upness = np.where(
        droop <= mapping.ear_rest,
        (mapping.ear_rest - droop) / (mapping.ear_rest - mapping.ear_up),
        -(droop - mapping.ear_rest) / (mapping.ear_down - mapping.ear_rest),
    ).clip(-1, 1)
    flick = np.abs(ears - lowpass(ears, mapping.flick_hz)).max(1) if len(ears) >= 16 else np.zeros(len(ears))
    points = np.array(mapping.grip_points)
    out[:, Ch.EXPAND] = mapping.expand_gain * upness
    out[:, Ch.GRIP] = (
        np.interp(upness, points[:, 0], points[:, 1])
        + mapping.flick_gain * np.maximum(0.0, flick - mapping.flick_deadband) / mapping.flick_full
    )
    slow = lowpass(out, mapping.detail_hz)
    return clip_to_limits(slow + np.array(mapping.detail_gain) * (out - slow))


def summary(motion: Frames) -> str:
    lines = []
    for j, key in enumerate(MOTION_KEYS):
        x = motion[:, j]
        lines.append(f"    {key:9s} mean {x.mean():+6.2f}  min {x.min():+6.2f}  max {x.max():+6.2f}")
    return "\n".join(lines)


def to_clip(name: str, prompt: str, motion: Frames) -> dict[str, Any]:
    """CONTRACTS §4 plan-space Clip JSON."""
    return {
        "name": name,
        "prompt": prompt,
        "idea": "retargeted from Reachy Mini",
        "recipe": "",
        "fps": FPS,
        "space": "plan",
        "channels": list(MOTION_KEYS),
        "frames": np.round(motion, 4).tolist(),
    }


def _library(names: list[str]) -> dict[str, ReachyTraj]:
    lib = pollen(EMOTIONS) | pollen(DANCES)
    missing = [n for n in names if n not in lib]
    if missing:
        raise SystemExit(f"unknown clips {missing}; e.g. {sorted(lib)[:8]}")
    return {n: lib[n] for n in names} if names else lib


def plot(motions: dict[str, Frames], out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(len(motions), 1, figsize=(11, 2.6 * len(motions)), squeeze=False)
    for ax, (name, m) in zip(axes[:, 0], motions.items(), strict=True):
        t = np.arange(len(m)) / FPS
        for j, key in enumerate(MOTION_KEYS):
            scale = {"orient": 1 / 60, "advance": 4.0}.get(key, 1.0)
            ax.plot(t, m[:, j] * scale, label=key + {"orient": " /60", "advance": " x4"}.get(key, ""))
        ax.set_title(name)
        ax.set_ylim(-1.05, 1.05)
        ax.grid(alpha=0.3)
    axes[0, 0].legend(ncol=8, fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(out, dpi=110)


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ml.retarget", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("describe")
    d.add_argument("names", nargs="+")
    c = sub.add_parser("clips")
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("names", nargs="*")
    p = sub.add_parser("plot")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("names", nargs="+")
    a = ap.parse_args()
    mapping = Mapping.load()
    motions = {n: retarget(t, mapping) for n, t in _library(a.names).items()}
    if a.cmd == "describe":
        for name, m in motions.items():
            print(f"  {name}  {len(m) / FPS:.1f} s\n{summary(m)}")
    elif a.cmd == "clips":
        a.out.mkdir(parents=True, exist_ok=True)
        captions = pollen_captions(EMOTIONS) | pollen_captions(DANCES)
        for name, m in motions.items():
            (a.out / f"{name}.json").write_text(json.dumps(to_clip(name, captions.get(name, name), m)))
        print(f"{len(motions)} clips -> {a.out}")
    else:
        plot(motions, a.out)
        print(f"-> {a.out}")


if __name__ == "__main__":
    main()
