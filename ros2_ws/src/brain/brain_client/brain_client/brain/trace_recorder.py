# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Persistent bounded agent trace used for physical-run postmortems."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from brain_client.common.script_paths import get_workspace_dir

_MAX_BYTES = 32 * 1024 * 1024
_BACKUPS = 3


class AgentTraceRecorder:
    def __init__(self, path: Path | None = None):
        self.path = path or get_workspace_dir() / "debug_runs" / "agent_trace.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def record(self, payload: str) -> None:
        """Persist one already-serialized lightweight trace event."""
        # Validate before touching disk: a malformed producer must not poison exports.
        json.loads(payload)
        encoded = (payload + "\n").encode()
        with self._lock:
            self._rotate_if_needed(len(encoded))
            with self.path.open("ab") as stream:
                stream.write(encoded)

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size + incoming <= _MAX_BYTES:
            return
        oldest = self.path.with_name(f"{self.path.name}.{_BACKUPS}")
        oldest.unlink(missing_ok=True)
        for index in range(_BACKUPS - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                source.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))
