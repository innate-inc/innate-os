# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import base64
import json
import runpy
import zipfile
from pathlib import Path

from brain_client.brain import trace_recorder
from brain_client.skills import debug_runs


def test_skill_debug_run_writes_manifest_events_and_terminal_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(debug_runs, "get_workspace_dir", lambda: tmp_path)
    run = debug_runs.SkillDebugRun(
        run_id="run-123",
        skill_id="innate-os/pull-held-handle",
        skill_name="pull_held_handle",
        inputs={"distance_m": 0.1},
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


def test_agent_trace_recorder_rotates_complete_jsonl_files(tmp_path, monkeypatch):
    monkeypatch.setattr(trace_recorder, "_MAX_BYTES", 80)
    recorder = trace_recorder.AgentTraceRecorder(tmp_path / "agent_trace.jsonl")

    recorder.record(json.dumps({"ev": "turn_end", "turn": 1, "thoughts": "first"}))
    recorder.record(json.dumps({"ev": "turn_end", "turn": 2, "thoughts": "second"}))

    current = [json.loads(line) for line in recorder.path.read_text().splitlines()]
    backup = [json.loads(line) for line in recorder.path.with_name("agent_trace.jsonl.1").read_text().splitlines()]
    assert current[-1]["turn"] == 2
    assert backup[-1]["turn"] == 1


def test_cli_export_correlates_agent_trace_and_extracts_frames(tmp_path, monkeypatch):
    os_root = tmp_path / "innate-os"
    workspace = os_root / "workspace"
    monkeypatch.setattr(debug_runs, "get_workspace_dir", lambda: workspace)
    run = debug_runs.SkillDebugRun(
        run_id="run-export", skill_id="innate-os/pull-held-handle", skill_name="pull_held_handle", inputs={}
    )
    run.event("step_decision", step=0)
    run.finish(status="success", message="done")
    trace_path = workspace / "debug_runs" / "agent_trace.jsonl"
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text(
        json.dumps(
            {
                "ev": "turn_start",
                "t": run.started_at,
                "turn": 7,
                "frames": [{"label": "wrist camera", "jpeg": base64.b64encode(b"jpeg-data").decode()}],
            }
        )
        + "\n"
        + json.dumps({"ev": "turn_end", "t": run.started_at + 0.1, "turn": 7, "thoughts": "pull"})
        + "\n"
    )

    script = Path(__file__).resolve().parents[5] / "scripts" / "innate"
    namespace = runpy.run_path(str(script))
    namespace["_export_skill_debug"].__globals__["INNATE_OS_ROOT"] = os_root
    destination = tmp_path / "bundle.zip"
    namespace["_export_skill_debug"](None, destination)

    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        exported_agent = archive.read("agent_trace.jsonl").decode()
        assert "skill/events.jsonl" in names
        assert "agent_frames/turn-7-0-wrist_camera.jpg" in names
        assert '"thoughts":"pull"' in exported_agent
        assert '"file":"agent_frames/turn-7-0-wrist_camera.jpg"' in exported_agent
        assert "jpeg-data" == archive.read("agent_frames/turn-7-0-wrist_camera.jpg").decode()
