# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Publish a recorded skill to the Hugging Face Hub as a LeRobotDataset, from the Datasets page.

The work is ``mars2lerobot`` from the LeRobot plugin (innate-os/lerobot), run as a low-priority
subprocess in that plugin's own Python 3.12 environment; the robot's system Python cannot hold
lerobot. It runs under this server rather than a ROS node because this process already owns the
keys and already serves the skill's files. One job at a time: an encode plus an upload is all the
spare CPU and uplink a robot has.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import sys
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

_INNATE_OS_ROOT = os.environ.get("INNATE_OS_ROOT", os.path.expanduser("~/innate-os"))
LEROBOT_DIR = Path(_INNATE_OS_ROOT) / "lerobot"
VENV_ENV = "INNATE_LEROBOT_VENV"
DATASET_METADATA = "dataset_metadata.json"
EXPORT_FILE = "lerobot_export.json"
REPO_ID_RE = re.compile(r"^[A-Za-z0-9][\w.\-]{0,95}/[A-Za-z0-9][\w.\-]{0,95}$")
_TAIL_LINES = 12
# Converting is most of the work; the Hub upload gets the last stretch of the bar.
_CONVERT_SHARE = 0.85


class Busy(Exception):
    """Another publish or setup job is still running."""


class LockUnreadable(Exception):
    """lerobot/uv.lock does not say which torch to install, so setup must not start."""


@dataclass
class Job:
    kind: str  # "publish" | "setup"
    dir: str = ""
    repo_id: str = ""
    stage: str = "starting"  # starting | converting | uploading | installing | done | error
    episode: int = 0
    total: int = 0
    progress: float = 0.0
    message: str = ""
    error: str = ""
    url: str = ""
    running: bool = True
    tail: deque = field(default_factory=lambda: deque(maxlen=_TAIL_LINES), repr=False)

    def public(self) -> dict:
        data = asdict(self)
        data.pop("tail")
        return data


_job: Job | None = None
_task: asyncio.Task | None = None


def venv_dir() -> Path:
    override = os.environ.get(VENV_ENV, "").strip()
    return Path(override) if override else LEROBOT_DIR / ".venv"


def converter() -> Path:
    return venv_dir() / "bin" / "mars2lerobot"


def env_ready() -> bool:
    return converter().is_file()


def current_job() -> dict | None:
    return _job.public() if _job is not None else None


def published(skill_dir: Path) -> dict | None:
    """What an earlier publish of this skill recorded, if anything (the converter's data/lerobot_export.json),
    and how many of those episodes have been deleted since, which makes the next publish a rebuild."""
    data_dir = skill_dir / "data"
    export = _json((data_dir if data_dir.is_dir() else skill_dir) / EXPORT_FILE)  # where the converter writes it
    if not export.get("repo_id"):
        return None
    published_ids = set(export.get("episode_ids", []))
    listed = _json(skill_dir / "data" / DATASET_METADATA).get("episodes")
    present = {ep.get("episode_id") for ep in listed if isinstance(ep, dict)} if isinstance(listed, list) else None
    return {
        "repo_id": export["repo_id"],
        "episodes": len(published_ids),
        "pushed": bool(export.get("pushed")),
        "removed": len(published_ids - present) if present is not None else 0,
    }


def _json(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def start_publish(skill_dir: Path, repo_id: str, *, private: bool, include_failures: bool, token: str) -> None:
    command = [str(converter()), str(skill_dir), "--repo-id", repo_id, "--push", "--progress-json"]
    if private:
        command.append("--private")
    if include_failures:
        command.append("--include-failures")
    job = Job(kind="publish", dir=str(skill_dir), repo_id=repo_id, message="Starting…")
    _start(job, [_niced(command)], {"HF_TOKEN": token, "HF_HUB_DISABLE_PROGRESS_BARS": "1"})


def start_setup() -> None:
    uv = _uv_command()
    steps = []
    if uv is None:
        steps.append([sys.executable, "-m", "pip", "install", "--user", "--quiet", "uv"])
        uv = [sys.executable, "-m", "uv"]
    locked = _locked_versions(LEROBOT_DIR / "uv.lock")
    if not all(name in locked for name in _TORCH):
        # Without the versions the sync below would pull PyPI's torch, 3.5 GB of CUDA a Jetson cannot
        # use, and the CPU install after it would then fail on an empty package list.
        raise LockUnreadable(f"could not read the torch versions from {LEROBOT_DIR / 'uv.lock'}")
    skipped = [name for name in locked if _GPU_ONLY.match(name) or name in _TORCH]
    sync = [*uv, "sync", "--frozen", "--no-default-groups", "--project", str(LEROBOT_DIR)]
    for name in skipped:
        sync += ["--no-install-package", name]
    steps.append(_niced(sync))
    # Converting a dataset never touches a GPU, and PyPI's Linux torch drags in ~3.5 GB of CUDA
    # libraries that a Jetson cannot even use; install the CPU build of the locked versions instead.
    python = str(venv_dir() / "bin" / "python")
    torch = [f"{name}=={locked[name]}" for name in _TORCH if name in locked]
    steps.append(_niced([*uv, "pip", "install", "--python", python, "--index-url", _TORCH_CPU_INDEX, *torch]))
    job = Job(kind="setup", stage="installing", message="Installing the LeRobot environment…")
    _start(job, steps, {"UV_PROJECT_ENVIRONMENT": str(venv_dir())})


_TORCH = ("torch", "torchvision")
_TORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
_GPU_ONLY = re.compile(r"^(nvidia-|cuda-|triton$)")
_LOCK_ENTRY = re.compile(r'^\[\[package\]\]\nname = "([^"]+)"\nversion = "([^"]+)"', re.MULTILINE)


def _locked_versions(lock: Path) -> dict[str, str]:
    try:
        return dict(_LOCK_ENTRY.findall(lock.read_text()))
    except OSError:
        return {}


def _start(job: Job, steps: list[list[str]], extra_env: dict[str, str]) -> None:
    global _job, _task
    if _job is not None and _job.running:
        raise Busy
    _job = job
    _task = asyncio.get_running_loop().create_task(_run(job, steps, {**os.environ, **extra_env}))


async def _run(job: Job, steps: list[list[str]], env: dict[str, str]) -> None:
    try:
        for command in steps:
            code = await _run_step(job, command, env)
            if code != 0:
                job.stage, job.error = "error", job.error or _last_words(job) or f"exited with code {code}"
                return
        if job.stage != "done":
            job.stage, job.progress = "done", 1.0
            job.message = "Ready" if job.kind == "setup" else job.message
    except Exception as e:  # noqa: BLE001 — a background job must end in a state the dialog can show
        job.stage, job.error = "error", str(e)
    finally:
        job.running = False


async def _run_step(job: Job, command: list[str], env: dict[str, str]) -> int:
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env
    )
    assert process.stdout is not None
    async for raw in process.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        if not _absorb_event(job, line):
            job.tail.append(line)
            if job.kind == "setup":
                job.message = line[:160]
    return await process.wait()


def _absorb_event(job: Job, line: str) -> bool:
    """Apply one ``--progress-json`` line to the job; False when the line is ordinary output."""
    if not line.startswith("{"):
        return False
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False
    kind = event.get("event")
    if kind == "start":
        job.stage, job.total = "converting", int(event.get("total", 0))
        job.message = f"Converting {job.total} episode{'s' if job.total != 1 else ''}…"
    elif kind == "episode":
        job.episode, job.total = int(event.get("index", 0)), int(event.get("total", job.total))
        job.progress = _CONVERT_SHARE * job.episode / max(job.total, 1)
        job.message = f"Converted episode {job.episode} of {job.total}"
    elif kind == "push":
        job.stage, job.progress, job.message = "uploading", _CONVERT_SHARE, "Uploading to Hugging Face…"
    elif kind == "done":
        job.stage, job.progress, job.url = "done", 1.0, str(event.get("url", ""))
        job.message = str(event.get("message", "Published"))
    elif kind == "error":
        job.error = str(event.get("message", ""))
    else:
        return False
    return True


def _last_words(job: Job) -> str:
    return " ".join(list(job.tail)[-3:])[:400]


def _uv_command() -> list[str] | None:
    found = shutil.which("uv")
    if found:
        return [found]
    with contextlib.suppress(ImportError):
        import uv  # noqa: F401 — presence check only

        return [sys.executable, "-m", "uv"]
    return None


def _niced(command: list[str]) -> list[str]:
    nice = shutil.which("nice")
    return [nice, "-n", "15", *command] if nice else command
