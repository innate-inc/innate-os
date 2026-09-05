# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import json
import runpy
import zipfile
from pathlib import Path

from brain_client.skills import debug_runs


def test_skill_debug_run_writes_manifest_events_and_terminal_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(debug_runs, "get_workspace_dir", lambda: tmp_path)
    run = debug_runs.SkillDebugRun(
        run_id="run-123",
        skill_id="innate-os/open_cabinet_with_gpt",
        skill_name="open_cabinet_with_gpt",
        inputs={"max_steps": 60},
    )

    run.event("step_decision", step=0, heading=[-1.0, 0.0, 0.0])
    run.finish(status="success", message="done")

    manifest = json.loads((run.directory / "manifest.json").read_text())
    events = [json.loads(line) for line in (run.directory / "events.jsonl").read_text().splitlines()]
    summary = json.loads((run.directory / "summary.json").read_text())
    assert manifest["run_id"] == "run-123"
    assert [event["event"] for event in events] == ["run_started", "step_decision", "run_finished"]
    assert [event["sequence"] for event in events] == [0, 1, 2]
    assert summary["status"] == "success"
    assert summary["events"] == 3


def test_cli_exports_skill_trace_and_camera_frames(tmp_path, monkeypatch):
    os_root = tmp_path / "innate-os"
    workspace = os_root / "workspace"
    monkeypatch.setattr(debug_runs, "get_workspace_dir", lambda: workspace)
    run = debug_runs.SkillDebugRun(
        run_id="run-export", skill_id="innate-os/open_cabinet_with_gpt", skill_name="open_cabinet_with_gpt", inputs={}
    )
    (run.directory / "00_head_gpt_0.jpg").write_bytes(b"jpeg-data")
    run.event("gpt_action", action="observe")
    run.finish(status="success", message="done")

    script = Path(__file__).resolve().parents[5] / "scripts" / "innate"
    namespace = runpy.run_path(str(script))
    namespace["_export_skill_debug"].__globals__["INNATE_OS_ROOT"] = os_root
    destination = tmp_path / "bundle.zip"
    namespace["_export_skill_debug"]("open_cabinet_with_gpt", destination)

    with zipfile.ZipFile(destination) as archive:
        assert "skill/events.jsonl" in archive.namelist()
        assert archive.read("skill/00_head_gpt_0.jpg") == b"jpeg-data"
        assert json.loads(archive.read("skill/summary.json"))["status"] == "success"
        assert json.loads(archive.read("export_manifest.json"))["run_id"] == "run-export"
