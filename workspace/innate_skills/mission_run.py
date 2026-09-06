# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Create a generic agent-owned run and preserve its skill artifacts."""

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from innate import Skill, SkillOutput, SkillReturn

ARTIFACT_RELATIVE_DIR = Path("workspace/skill_storage/household_orders")
ACTIVE_RUN_FILENAME = "active_run.json"


def _root() -> Path:
    return Path(os.environ.get("INNATE_OS_ROOT", Path.home() / "innate-os"))


def artifact_root() -> Path:
    return _root() / ARTIFACT_RELATIVE_DIR


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def active_run() -> dict[str, Any] | None:
    try:
        raw = json.loads((artifact_root() / ACTIVE_RUN_FILENAME).read_text())
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != 1:
        return None
    run_id = raw.get("run_id")
    if not isinstance(run_id, str) or not run_id or Path(run_id).name != run_id:
        return None
    return raw


def active_run_id() -> str | None:
    run = active_run()
    return run["run_id"] if run is not None else None


def start_run(agent_id: str) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    run_id = f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    run = {
        "version": 1,
        "run_id": run_id,
        "agent_id": agent_id,
        "started_at": started_at.isoformat(),
    }
    encoded = json.dumps(run, indent=2).encode()
    root = artifact_root()
    _atomic_write(root / "runs" / run_id / "run.json", encoded)
    _atomic_write(root / ACTIVE_RUN_FILENAME, encoded)
    return run


def write_artifact_set(kind: str, artifacts: dict[str, bytes], *, snapshot: bool = True) -> None:
    """Write latest copies and one immutable, same-token snapshot set."""
    root = artifact_root()
    run_id = active_run_id()
    for filename, payload in artifacts.items():
        _atomic_write(root / filename, payload)
        if run_id is not None:
            _atomic_write(root / "runs" / run_id / "latest" / filename, payload)
    if not snapshot or run_id is None:
        return
    token = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}-{uuid.uuid4().hex[:6]}"
    snapshot_dir = root / "runs" / run_id / "snapshots" / kind / token
    for filename, payload in artifacts.items():
        _atomic_write(snapshot_dir / filename, payload)


class MissionRun(Skill):
    """Start a fresh generic run shared by this agent's stateful skills."""

    def execute(self) -> SkillReturn:
        run = start_run("household_orders_agent")
        return SkillOutput(
            f"RUN_STARTED {json.dumps({'run_id': run['run_id']}, separators=(',', ':'))}",
            data={"run_id": run["run_id"]},
        )
