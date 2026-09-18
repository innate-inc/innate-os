# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""``meta/mars.json``: what a MARS dataset needs to say that LeRobot's schema has no place for.

lerobot's ``info.json`` drops keys it does not know, so robot-specific facts ride in a sidecar
inside ``meta/``; ``push_to_hub`` uploads it with everything else. Today that is the head tilt,
which decides what the head camera sees and must match between recording and rollout.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from lerobot.utils.constants import HF_LEROBOT_HOME

SIDECAR = Path("meta") / "mars.json"
DEFAULT_HEAD_ANGLE_DEG = -20.0
RECENT_S = 300.0


def read_sidecar(root: Path) -> dict | None:
    path = root / SIDECAR
    if not path.is_file():
        return None
    with open(path) as f:
        return json.load(f)


def write_sidecar(root: Path, *, head_angle_deg: float | None, source: str, head_angle_assumed: bool = False) -> Path:
    path = root / SIDECAR
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "robot": "mars",
        "head_angle_deg": head_angle_deg,
        "head_angle_assumed": head_angle_assumed,
        "source": source,
        "plugin_version": _plugin_version(),
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return path


def recording_root(argv: Sequence[str], now: float | None = None) -> Path | None:
    """The dataset a lerobot script in this process is writing, from its own command line.

    ``--dataset.root`` names it outright. Otherwise lerobot put it under ``$HF_LEROBOT_HOME/<repo_id>``,
    possibly with a date-time stamp appended, so the newest matching dataset created in the last few
    minutes is the one. A replayed dataset is older than that and is left alone; a resumed one
    (``--resume=true``) keeps its exact name, so it is found by name whatever its age.
    """
    root = _flag(argv, "--dataset.root")
    if root:
        path = Path(root).expanduser()
        return path if (path / "meta" / "info.json").is_file() else None
    repo_id = _flag(argv, "--dataset.repo_id")
    if not repo_id or "/" not in repo_id:
        return None
    owner, name = repo_id.split("/", 1)
    parent = HF_LEROBOT_HOME / owner
    if not parent.is_dir():
        return None
    if (_flag(argv, "--resume") or "").lower() == "true":
        resumed = parent / name
        return resumed if (resumed / "meta" / "info.json").is_file() else None
    now = time.time() if now is None else now
    fresh = [
        info.parent.parent
        for info in parent.glob("*/meta/info.json")
        if (info.parent.parent.name == name or info.parent.parent.name.startswith(name + "_"))
        and now - info.stat().st_mtime <= RECENT_S
    ]
    return max(fresh, key=lambda d: (d / "meta" / "info.json").stat().st_mtime, default=None)


def _flag(argv: Sequence[str], name: str) -> str | None:
    for i, arg in enumerate(argv):
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
        if arg == name and i + 1 < len(argv):
            return argv[i + 1]
    return None


def _plugin_version() -> str:
    try:
        return version("lerobot_robot_mars")
    except PackageNotFoundError:
        return "unknown"
