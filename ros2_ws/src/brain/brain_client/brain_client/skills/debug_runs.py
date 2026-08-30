# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Bounded, export-friendly traces for physical skill runs."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from brain_client.common.script_paths import get_workspace_dir

_MAX_RUNS_PER_SKILL = 50
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_component(value: str) -> str:
    return _SAFE_COMPONENT.sub("_", value).strip("._") or "unknown"


class SkillDebugRun:
    """Append-only JSONL recorder, created only when a skill emits debug data."""

    def __init__(self, *, run_id: str, skill_id: str, skill_name: str, inputs: dict):
        self.run_id = _safe_component(run_id)
        self.skill_id = skill_id
        self.skill_name = skill_name
        self.started_at = time.time()
        self.directory = get_workspace_dir() / "debug_runs" / "skills" / _safe_component(skill_name) / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self._lock = threading.Lock()
        self._sequence = 0
        self._events = (self.directory / "events.jsonl").open("a", encoding="utf-8", buffering=1)
        self._write_json(
            self.directory / "manifest.json",
            {
                "schema_version": 1,
                "run_id": run_id,
                "skill_id": skill_id,
                "skill_name": skill_name,
                "started_at": self.started_at,
                "inputs": inputs,
            },
        )
        self.event("run_started", inputs=inputs)
        self._prune_old_runs()

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)

    def event(self, event: str, **fields) -> None:
        with self._lock:
            payload = {
                "schema_version": 1,
                "run_id": self.run_id,
                "sequence": self._sequence,
                "timestamp": time.time(),
                "monotonic": time.monotonic(),
                "event": event,
                **fields,
            }
            self._sequence += 1
            self._events.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")

    def finish(self, *, status: str, message: str) -> None:
        with self._lock:
            if self._events.closed:
                return
        self.event("run_finished", status=status, message=message)
        finished_at = time.time()
        self._write_json(
            self.directory / "summary.json",
            {
                "schema_version": 1,
                "run_id": self.run_id,
                "skill_id": self.skill_id,
                "skill_name": self.skill_name,
                "started_at": self.started_at,
                "finished_at": finished_at,
                "duration_s": finished_at - self.started_at,
                "status": status,
                "message": message,
                "events": self._sequence,
            },
        )
        with self._lock:
            self._events.close()

    def _prune_old_runs(self) -> None:
        siblings = sorted(
            (path for path in self.directory.parent.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for old in siblings[_MAX_RUNS_PER_SKILL:]:
            for child in old.iterdir():
                if child.is_file():
                    child.unlink()
            try:
                old.rmdir()
            except OSError:
                pass
