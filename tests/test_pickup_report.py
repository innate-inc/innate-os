"""The benchmark must reject mixed working-tree code at one Git revision."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pickup_report", REPO / "scripts/experiments/pickup/report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def test_report_rejects_missing_or_mismatched_frozen_source_hashes(tmp_path):
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    hashes = report.expected_source_hashes(revision)
    scenarios = json.loads((REPO / "scripts/experiments/pickup/scenarios.json").read_text())
    manifests = []
    for scenario in scenarios:
        for controller in ("classic", "astra"):
            for repeat in (1, 2, 3):
                trial = tmp_path / f"{scenario['id']}-{controller}-{repeat}"
                trial.mkdir()
                manifest = {
                    "scenario": {
                        **scenario,
                        "scenario_id": scenario["id"],
                        "campaign": "fixture",
                        "controller": controller,
                        "repeat": repeat,
                    },
                    "source_revision": revision,
                    "source_unchanged_during_trial": True,
                    "recording_errors": [],
                    "sha256": hashes,
                }
                path = trial / "manifest.json"
                path.write_text(json.dumps(manifest))
                manifests.append((path, manifest))
                latency = 60 if controller == "classic" else 20
                (trial / "judgement.json").write_text(
                    json.dumps(
                        {
                            "success": True,
                            "end_to_end_s": latency,
                            "action_elapsed_s": latency - 2,
                            "physics_to_wall_ratio": 1.0,
                        }
                    )
                )
    assert report.build_report(tmp_path, "fixture")["meets_benchmark_criteria"]
    path, manifest = manifests[0]
    key = next(iter(hashes))
    for invalid in ({}, {k: v for k, v in hashes.items() if k != key}, {**hashes, key: "0" * 64}):
        path.write_text(json.dumps({**manifest, "sha256": invalid}))
        result = report.build_report(tmp_path, "fixture")
        assert not result["complete"] and not result["meets_benchmark_criteria"]
    # Agreement with the first manifest is insufficient: all runs can share
    # the same uncommitted change while claiming the frozen Git revision.
    for path, manifest in manifests:
        path.write_text(json.dumps({**manifest, "sha256": {**hashes, key: "0" * 64}}))
    assert not report.build_report(tmp_path, "fixture")["meets_benchmark_criteria"]


def test_judge_does_not_count_cancelled_ros_success_as_a_pickup(tmp_path):
    (tmp_path / "manifest.json").write_text(
        json.dumps(
            {
                "scenario": {"prop": "brick"},
                "initial_state": {"objects": {"brick": [0, 0, 0]}},
            }
        )
    )
    samples = [
        {
            "t": t,
            "observed_wall": 100 + t,
            "pose": [0, 0, 0],
            "objects": {"brick": [0.2, 0, 0.1]},
            "joints": {"joint6": 0.3},
        }
        for t in (0, 2.1, 20.1)
    ]
    (tmp_path / "world.jsonl").write_text("\n".join(json.dumps(s) for s in samples))
    for extra, expected in (
        ({"success_type": "success"}, True),
        ({"success_type": "cancelled"}, False),
        ({"cancel_wall": 99}, False),
    ):
        (tmp_path / "result.json").write_text(
            json.dumps(
                {
                    "success": True,
                    "status": 4,
                    "completion_wall": 100,
                    "action_elapsed_s": 10,
                    **extra,
                }
            )
        )
        subprocess.run(
            [sys.executable, str(REPO / "scripts/experiments/pickup/judge.py"), str(tmp_path)],
            check=True,
            capture_output=True,
        )
        result = json.loads((tmp_path / "judgement.json").read_text())
        assert result["stable_hold"] and result["durable_hold_20s"]
        assert result["success"] is expected
