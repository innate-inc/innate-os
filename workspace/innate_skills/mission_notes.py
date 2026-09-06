# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Store exact, run-scoped facts that must survive a long agent context."""

import json
from typing import Any

from innate_skills.mission_run import active_run_id, write_artifact_set

from innate import Skill, SkillOutput, SkillReturn

STATE_VERSION = 1
MAX_KEY_LENGTH = 120
MAX_VALUE_LENGTH = 4_000


def _result(code: str, payload: dict[str, Any] | None = None) -> SkillOutput:
    data = payload or {}
    return SkillOutput(
        f"{code} {json.dumps(data, separators=(',', ':'), ensure_ascii=False)}",
        data=data,
    )


class MissionNotes(Skill):
    """A small exact-text notebook for the active mission run.

    ``set`` saves or replaces one key, ``get`` retrieves one key, ``list``
    returns every note, and ``delete`` removes one key. The skill stores facts;
    it does not decide what the agent should do with them.
    """

    @staticmethod
    def _fresh_state(run_id: str) -> dict[str, Any]:
        return {"version": STATE_VERSION, "run_id": run_id, "notes": {}}

    def _load_state(self, run_id: str) -> dict[str, Any]:
        raw = self.storage.get("state")
        if (
            not isinstance(raw, dict)
            or raw.get("version") != STATE_VERSION
            or raw.get("run_id") != run_id
            or not isinstance(raw.get("notes"), dict)
        ):
            return self._fresh_state(run_id)
        notes = {key: value for key, value in raw["notes"].items() if isinstance(key, str) and isinstance(value, str)}
        return {"version": STATE_VERSION, "run_id": run_id, "notes": notes}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.storage["state"] = state
        write_artifact_set(
            "notes",
            {"mission_notes.json": json.dumps(state, indent=2, ensure_ascii=False).encode()},
        )

    @staticmethod
    def _key(value: str | None) -> str:
        return value.strip()[:MAX_KEY_LENGTH] if isinstance(value, str) else ""

    @staticmethod
    def _value(value: str | None) -> str:
        return value.strip()[:MAX_VALUE_LENGTH] if isinstance(value, str) else ""

    def execute(self, action: str, key: str | None = None, value: str | None = None) -> SkillReturn:
        run_id = active_run_id()
        if run_id is None:
            return _result("RUN_UNINITIALIZED")

        action = action.strip().lower() if isinstance(action, str) else ""
        state = self._load_state(run_id)
        notes = state["notes"]

        if action == "list":
            return _result("MISSION_NOTES", {"notes": notes})

        note_key = self._key(key)
        if not note_key:
            return _result("NOTE_NOT_CHANGED", {"reason": "non_empty_key_required"})

        if action == "get":
            if note_key not in notes:
                return _result("NOTE_MISSING", {"key": note_key})
            return _result("NOTE_FOUND", {"key": note_key, "value": notes[note_key]})

        if action == "set":
            note_value = self._value(value)
            if not note_value:
                return _result("NOTE_NOT_CHANGED", {"reason": "non_empty_value_required", "key": note_key})
            notes[note_key] = note_value
            self._save_state(state)
            return _result("NOTE_SAVED", {"key": note_key, "value": note_value})

        if action == "delete":
            existed = notes.pop(note_key, None) is not None
            if existed:
                self._save_state(state)
            return _result("NOTE_DELETED" if existed else "NOTE_MISSING", {"key": note_key})

        self.fail("Unknown action. Use one of: set, get, list, delete.")
