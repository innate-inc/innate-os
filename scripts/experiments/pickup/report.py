"""Report every frozen matched attempt, including the fixed failure penalty."""

import argparse
import ast
import hashlib
import json
import re
import statistics
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def expected_source_hashes(revision):
    """Reconstruct the exact recorded sources from the frozen Git revision.

    Read the source list and overlay bytes from that revision's runner, so a
    later report change cannot silently bless a different experiment overlay.
    No recorded working-tree hash is trusted as the expected implementation.
    """
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Missing or invalid frozen source revision")

    def read(relative):
        return subprocess.check_output(["git", "show", f"{revision}:{relative}"], cwd=REPO, stderr=subprocess.DEVNULL)

    tree = ast.parse(read("scripts/experiments/pickup/run_trial.py"))
    sources = suffix = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if "sources" in names:
            sources = ast.literal_eval(node.value)
        elif "instrumented" in names and isinstance(node.value, ast.BinOp) and isinstance(node.value.op, ast.Add):
            suffix = ast.literal_eval(node.value.right)
    if not isinstance(sources, list) or not sources or not isinstance(suffix, bytes):
        raise ValueError("Frozen runner source recipe is missing or unsupported")
    if len(sources) != len(set(sources)) or any(
        not isinstance(p, str) or p.startswith("/") or ".." in Path(p).parts for p in sources
    ):
        raise ValueError("Invalid frozen source paths")
    skill = "workspace/innate_skills/pick_any_object.py"
    helper = "workspace/innate_skills/_pickup_probe.py"
    if skill not in sources or helper not in sources:
        raise ValueError("Frozen source recipe lacks the skill or instrumentation")
    hashes = {}
    for relative in sources:
        data = read("scripts/experiments/pickup/probe.py" if relative == helper else relative)
        if relative == skill:
            data += suffix
        hashes[relative] = hashlib.sha256(data).hexdigest()
    return hashes


def build_report(root, campaign):
    scenarios = json.loads(Path(__file__).with_name("scenarios.json").read_text())
    rows = []
    paths = []
    for path in root.glob("*/manifest.json"):
        manifest = json.loads(path.read_text())
        if manifest["scenario"].get("campaign") == campaign:
            paths.append((path, manifest))
    revisions = {m.get("source_revision") for _, m in paths}
    expected = None
    source_error = None
    try:
        if len(revisions) != 1:
            raise ValueError("Matched trials must share one frozen revision")
        expected = expected_source_hashes(next(iter(revisions)))
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        source_error = str(error)
    for path, manifest in paths:
        scenario = manifest["scenario"]
        if scenario.get("campaign") != campaign:
            continue
        judgement = json.loads(path.with_name("judgement.json").read_text())
        expected_scenario = next((s for s in scenarios if s["id"] == scenario["scenario_id"]), {})
        valid = (
            bool(expected_scenario)
            and expected is not None
            and manifest.get("sha256") == expected
            and all(scenario.get(k) == v for k, v in expected_scenario.items() if k != "id")
            and manifest.get("source_unchanged_during_trial") is True
            and not manifest.get("recording_errors")
            and 0.98 <= judgement.get("physics_to_wall_ratio", 0) <= 1.02
        )
        rows.append(
            {
                "trial": path.parent.name,
                "controller": scenario["controller"],
                "scenario": scenario["scenario_id"],
                "repeat": scenario["repeat"],
                "success": judgement["success"],
                "latency_s": judgement["end_to_end_s"],
                "penalized_s": judgement["end_to_end_s"] if judgement["success"] else 180,
                "action_s": judgement["action_elapsed_s"],
                "revision": manifest["source_revision"],
                "valid_comparison": valid,
            }
        )

    by_scenario = {}
    complete = all(r["valid_comparison"] for r in rows) and len({r["revision"] for r in rows}) == 1
    for scenario in scenarios:
        entries = [r for r in rows if r["scenario"] == scenario["id"]]
        by_scenario[scenario["id"]] = {}
        for controller in ("classic", "astra"):
            values = [r for r in entries if r["controller"] == controller]
            complete = complete and sorted(r["repeat"] for r in values) == [1, 2, 3]
            successes = [r["latency_s"] for r in values if r["success"]]
            by_scenario[scenario["id"]][controller] = {
                "attempts": len(values),
                "successes": len(successes),
                "success_median_s": statistics.median(successes) if successes else None,
                "all_attempt_median_s": statistics.median(r["penalized_s"] for r in values) if values else None,
            }

    medians = {}
    for controller in ("classic", "astra"):
        entries = [r for r in rows if r["controller"] == controller]
        successes = [r["latency_s"] for r in entries if r["success"]]
        medians[controller] = {
            "success_median_s": statistics.median(successes) if successes else None,
            "all_attempt_median_s": statistics.median(r["penalized_s"] for r in entries) if entries else None,
        }
    ratios = {
        key: medians["astra"][key] / medians["classic"][key]
        if medians["astra"][key] is not None and medians["classic"][key]
        else None
        for key in ("success_median_s", "all_attempt_median_s")
    }
    working_baseline = complete and all(s["classic"]["successes"] >= 2 for s in by_scenario.values())
    reliability_preserved = complete and all(
        s["astra"]["successes"] >= s["classic"]["successes"] for s in by_scenario.values()
    )
    result = {
        "campaign": campaign,
        "frozen_source_hashes": expected,
        "source_audit_error": source_error,
        "complete": complete,
        "working_baseline": working_baseline,
        "observed_reliability_preserved": reliability_preserved,
        "medians": medians,
        "candidate_to_baseline_ratio": ratios,
        "by_scenario": by_scenario,
        "trials": rows,
        "meets_benchmark_criteria": working_baseline
        and reliability_preserved
        and all(v is not None and v <= 0.5 for v in ratios.values()),
        "uncertainty": "Three repeats per scenario detect obvious regressions; they cannot establish population reliability or hardware performance.",
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--campaign", required=True)
    args = parser.parse_args()
    result = build_report(args.root, args.campaign)
    (args.root / f"{args.campaign}-report.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
