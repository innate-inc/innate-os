# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Focused tests for generic run-scoped mission notes."""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "workspace"))

from innate_skills import mission_notes as notes_module  # noqa: E402
from innate_skills import mission_run as run_module  # noqa: E402


def _payload(output, code: str) -> dict:
    actual, encoded = output.message.split(" ", 1)
    assert actual == code
    assert output.data == json.loads(encoded)
    return output.data


def test_notes_require_an_active_run(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    skill = notes_module.MissionNotes(None)

    _payload(skill.execute("list"), "RUN_UNINITIALIZED")


def test_notes_preserve_exact_text_and_archive_per_run(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    first_run = run_module.start_run("test_agent")["run_id"]
    skill = notes_module.MissionNotes(None)
    exact = "Sweetgreen Harvest Bowl — roasted chicken, no goat cheese; vinaigrette on the side."

    saved = _payload(skill.execute("set", "person-002.confirmed_order", exact), "NOTE_SAVED")
    assert saved["value"] == exact
    assert _payload(skill.execute("get", "person-002.confirmed_order"), "NOTE_FOUND")["value"] == exact
    assert _payload(skill.execute("list"), "MISSION_NOTES")["notes"] == {"person-002.confirmed_order": exact}

    artifact = (
        tmp_path
        / "workspace"
        / "skill_storage"
        / "household_orders"
        / "runs"
        / first_run
        / "latest"
        / "mission_notes.json"
    )
    assert json.loads(artifact.read_text())["notes"]["person-002.confirmed_order"] == exact

    run_module.start_run("test_agent")
    assert _payload(skill.execute("list"), "MISSION_NOTES")["notes"] == {}


def test_notes_can_be_replaced_and_deleted(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    run_module.start_run("test_agent")
    skill = notes_module.MissionNotes(None)

    _payload(skill.execute("set", "answer", "first"), "NOTE_SAVED")
    _payload(skill.execute("set", "answer", "corrected"), "NOTE_SAVED")
    assert _payload(skill.execute("get", "answer"), "NOTE_FOUND")["value"] == "corrected"
    _payload(skill.execute("delete", "answer"), "NOTE_DELETED")
    _payload(skill.execute("get", "answer"), "NOTE_MISSING")
