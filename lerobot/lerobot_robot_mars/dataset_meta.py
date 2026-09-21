# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""``meta/mars.json``: what a MARS dataset needs to say that LeRobot's schema has no place for.

lerobot's ``info.json`` drops keys it does not know, so robot-specific facts ride in a sidecar
inside ``meta/``; ``push_to_hub`` uploads it with everything else. Today that is the head tilt,
which decides what the head camera sees and must match between recording and rollout.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from huggingface_hub import hf_hub_download
from huggingface_hub.errors import HfHubHTTPError
from lerobot.utils.constants import HF_LEROBOT_HOME

from .hub import has_dataset

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
        return path if has_dataset(path) else None
    repo_id = _flag(argv, "--dataset.repo_id")
    if not repo_id or "/" not in repo_id:
        return None
    owner, name = repo_id.split("/", 1)
    parent = HF_LEROBOT_HOME / owner
    if not parent.is_dir():
        return None
    if (_flag(argv, "--resume") or "").lower() == "true":
        resumed = parent / name
        return resumed if has_dataset(resumed) else None
    now = time.time() if now is None else now
    # The name itself or lerobot's `_YYYYMMDD_HHMMSS` stamp on it; a bare prefix would also match a
    # sibling dataset ("pick" and "pick_tv") and label it with this run's head angle.
    stamped = re.compile(re.escape(name) + r"(_\d{8}_\d{6})?")
    fresh = [
        info.parent.parent
        for info in parent.glob("*/meta/info.json")
        if stamped.fullmatch(info.parent.parent.name) and now - info.stat().st_mtime <= RECENT_S
    ]
    return max(fresh, key=lambda d: (d / "meta" / "info.json").stat().st_mtime, default=None)


def head_angle_for_run(argv: Sequence[str]) -> tuple[float, str] | None:
    """The head tilt of the dataset behind this run, with where it was found; None if unknown.

    A run relates to a dataset in one of two ways: it names one (resuming a recording, replaying an
    episode), or it runs a policy, whose checkpoint records the dataset it was trained on. Either
    way that dataset's ``meta/mars.json`` says how the head was tilted when the frames were taken,
    which is how it must be tilted now.
    """
    for repo_id, root in (_named_dataset(argv), _policy_dataset(argv)):
        sidecar = _sidecar(repo_id, root)
        angle = sidecar.get("head_angle_deg") if sidecar else None
        if isinstance(angle, int | float):
            return float(angle), repo_id or str(root)
    return None


def _named_dataset(argv: Sequence[str]) -> tuple[str | None, Path | None]:
    root = _flag(argv, "--dataset.root")
    return _flag(argv, "--dataset.repo_id"), Path(root).expanduser() if root else None


def _policy_dataset(argv: Sequence[str]) -> tuple[str | None, Path | None]:
    policy = _flag(argv, "--policy.path")
    if not policy:
        return None, None
    local = Path(policy).expanduser() / "train_config.json"
    config_path = local if local.is_file() else _from_hub(policy, "train_config.json", "model")
    if config_path is None:
        return None, None
    try:
        dataset = json.loads(config_path.read_text()).get("dataset", {})
    except (OSError, json.JSONDecodeError):
        return None, None
    root = dataset.get("root")
    return dataset.get("repo_id"), Path(root).expanduser() if root else None


def _sidecar(repo_id: str | None, root: Path | None) -> dict | None:
    for local in (root, HF_LEROBOT_HOME / repo_id if repo_id else None):
        if local is not None and (found := read_sidecar(local)) is not None:
            return found
    if not repo_id:
        return None
    path = _from_hub(repo_id, str(SIDECAR), "dataset")
    try:
        return json.loads(path.read_text()) if path else None
    except (OSError, json.JSONDecodeError):
        return None


def _from_hub(repo_id: str, filename: str, repo_type: str) -> Path | None:
    try:
        return Path(hf_hub_download(repo_id, filename, repo_type=repo_type))
    except (HfHubHTTPError, OSError, ValueError):  # not on the Hub, no such file, offline, or not a repo id
        return None


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
