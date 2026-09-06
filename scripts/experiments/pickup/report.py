"""Report every frozen matched attempt, including the fixed failure penalty."""

import argparse
import json
import statistics
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("root", type=Path)
parser.add_argument("--campaign", required=True)
a = parser.parse_args()
scenarios = json.loads(Path(__file__).with_name("scenarios.json").read_text())
rows = []
for path in a.root.glob("*/manifest.json"):
    manifest = json.loads(path.read_text())
    scenario = manifest["scenario"]
    if scenario.get("campaign") != a.campaign:
        continue
    judgement = json.loads(path.with_name("judgement.json").read_text())
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
        }
    )

by_scenario = {}
complete = True
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
    "campaign": a.campaign,
    "complete": complete,
    "working_baseline": working_baseline,
    "observed_reliability_preserved": reliability_preserved,
    "medians": medians,
    "candidate_to_baseline_ratio": ratios,
    "by_scenario": by_scenario,
    "trials": rows,
    "goal_passed": working_baseline
    and reliability_preserved
    and all(v is not None and v <= 0.5 for v in ratios.values()),
    "uncertainty": "Three repeats per scenario detect obvious regressions; they cannot establish population reliability or hardware performance.",
}
(a.root / f"{a.campaign}-report.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
