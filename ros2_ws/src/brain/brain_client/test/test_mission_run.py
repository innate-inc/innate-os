# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Mission retries must not erase progress; an explicit new task must."""

import json
import multiprocessing
import sys
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from brain_client.skills.types import SkillCancelled, SkillFailed

REPO_ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(REPO_ROOT / "workspace"))

from innate_skills import mission_run as run_module  # noqa: E402
from innate_skills.find_next_person import FindNextPerson  # noqa: E402
from innate_skills.mission_notes import MissionNotes  # noqa: E402
from innate_skills.mission_run import MissionRun, active_run_id, artifact_root, start_run  # noqa: E402
from innate_skills.person_identity import PersonIdentity  # noqa: E402


def test_retry_preserves_identity_notes_search_and_archives(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    # Compatibility with a mission started by the old skill or an external runner.
    run_id = start_run("household_orders_agent")["run_id"]
    identity, notes, search = PersonIdentity(None), MissionNotes(None), FindNextPerson(None)
    identity.execute("begin")
    search.execute(reset=True)
    roster = identity.storage["state"]
    roster["encounters"] = [{"encounter_id": "person-001", "status": "seen", "reference_images_b64": []}]
    identity._save_state(roster)
    saved_note = '{"name":"Sam","confirmed_order":"no onions; sauce on the side"}'
    notes.execute("set", "person-001", saved_note)
    coverage = search.storage["state"]
    coverage["observations"] = [{"x": 1.0, "y": 2.0, "theta": 0.5}]
    search.storage["state"] = coverage
    archives = {p: p.read_bytes() for p in (artifact_root() / "runs").rglob("*") if p.is_file()}

    # The historical failure sequence: a repeated mission_run({}), then begin/reset.
    # Fresh instances also exercise persistence after a skill reload.
    retried = MissionRun(None).execute()
    assert retried.message.startswith("RUN_RESUMED ")
    assert retried.data["run_id"] == active_run_id() == run_id
    assert PersonIdentity(None).execute("begin").message.startswith("ROSTER_ALREADY_INITIALIZED ")
    assert FindNextPerson(None).execute(reset=True).data["observations"] == 1
    assert MissionNotes(None).execute("list").data == {"notes": {"person-001": saved_note}}
    assert len(PersonIdentity(None)._load_state()["encounters"]) == 1
    assert {p: p.read_bytes() for p in (artifact_root() / "runs").rglob("*") if p.is_file()} == archives

    # A fresh challenge/explicit new task still starts with no inherited facts.
    new_run = MissionRun(None).execute(restart=True).data["run_id"]
    assert new_run != run_id
    assert PersonIdentity(None).execute("begin").message.startswith("ROSTER_INITIALIZED ")
    assert PersonIdentity(None)._load_state()["encounters"] == []
    assert FindNextPerson(None).execute(reset=True).message.startswith("SEARCH_RESET ")
    assert FindNextPerson(None).storage["state"]["observations"] == []
    assert MissionNotes(None).execute("list").data == {"notes": {}}
    assert all(p.read_bytes() == contents for p, contents in archives.items())


def test_resume_completes_interrupted_startup_without_reusing_previous_mission_state(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    previous_run = start_run("household_orders_agent")["run_id"]
    identity, search = PersonIdentity(None), FindNextPerson(None)
    identity.execute("begin")
    search.execute(reset=True)
    roster = identity.storage["state"]
    roster["encounters"] = [{"encounter_id": "person-001", "status": "seen", "reference_images_b64": []}]
    identity._save_state(roster)
    coverage = search.storage["state"]
    coverage["observations"] = [{"x": 1.0, "y": 2.0, "theta": 0.5}]
    search.storage["state"] = coverage
    MissionNotes(None).execute("set", "person-001", "previous mission order")

    # Startup was interrupted after committing the new run, before either initializer.
    new_run = MissionRun(None).execute(restart=True).data["run_id"]
    assert new_run != previous_run
    assert PersonIdentity(None)._load_state()["run_id"] == previous_run
    assert FindNextPerson(None).storage["state"]["run_id"] == previous_run

    # Follow the recovery sequence using fresh instances, as after a reload.
    resumed = MissionRun(None).execute()
    assert resumed.message.startswith("RUN_RESUMED ")
    assert resumed.data["run_id"] == new_run
    assert PersonIdentity(None).execute("begin").message.startswith("ROSTER_INITIALIZED ")
    assert FindNextPerson(None).execute(reset=True).message.startswith("SEARCH_RESET ")
    assert MissionNotes(None).execute("list").data == {"notes": {}}
    assert PersonIdentity(None)._load_state()["run_id"] == new_run
    assert PersonIdentity(None)._load_state()["encounters"] == []
    assert FindNextPerson(None).storage["state"]["run_id"] == new_run
    assert FindNextPerson(None).storage["state"]["observations"] == []
    assert len(list((artifact_root() / "runs").iterdir())) == 2


def _initialize_mission(_):
    return MissionRun(None).execute()


def test_concurrent_process_initialization_creates_one_run(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    # Fork preserves the candidate module selected by the test runner.
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("fork")) as pool:
        outputs = list(pool.map(_initialize_mission, range(8)))
    assert len({output.data["run_id"] for output in outputs}) == 1
    assert sum(output.message.startswith("RUN_STARTED ") for output in outputs) == 1
    assert len(list((artifact_root() / "runs").iterdir())) == 1


@pytest.mark.parametrize("restart", ["false", 1, None])
def test_invalid_restart_cannot_discard_current_mission(tmp_path, monkeypatch, restart):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    first = start_run("household_orders_agent")
    with pytest.raises(SkillFailed, match="restart must be a boolean"):
        MissionRun(None).execute(restart=restart)
    assert active_run_id() == first["run_id"]
    assert json.loads((artifact_root() / "active_run.json").read_text()) == first


def test_other_agents_run_requires_explicit_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    first = start_run("other_agent")
    with pytest.raises(SkillFailed, match="different agent"):
        MissionRun(None).execute()
    assert active_run_id() == first["run_id"]
    assert len(list((artifact_root() / "runs").iterdir())) == 1
    output = MissionRun(None).execute(restart=True)
    assert output.message.startswith("RUN_STARTED ")
    assert output.data["run_id"] != first["run_id"]


def test_already_cancelled_start_does_not_create_state(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    skill = MissionRun(None)
    skill.cancel()
    with pytest.raises(SkillCancelled):
        skill.execute()
    assert not artifact_root().exists()


def test_cancel_while_acquiring_lock_cannot_restart_mission(tmp_path, monkeypatch):
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))
    start_run("household_orders_agent")
    before = {p: p.read_bytes() for p in artifact_root().rglob("*") if p.is_file()}
    skill = MissionRun(None)
    original_lock = run_module._run_lock

    @contextmanager
    def cancel_before_lock_is_acquired():
        skill.cancel()
        with original_lock():
            yield

    monkeypatch.setattr(run_module, "_run_lock", cancel_before_lock_is_acquired)
    with pytest.raises(SkillCancelled):
        skill.execute(restart=True)
    assert {p: p.read_bytes() for p in artifact_root().rglob("*") if p.is_file()} == before
