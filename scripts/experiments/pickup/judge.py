"""Judge isolated floor-prop trials; never feeds model inputs."""

import argparse
import json
import math
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument("trial", type=Path)
a = p.parse_args()
result = json.loads((a.trial / "result.json").read_text())
manifest = json.loads((a.trial / "manifest.json").read_text())
prop = manifest["scenario"]["prop"]
samples = [json.loads(line) for line in (a.trial / "world.jsonl").read_text().splitlines()]
samples = [sample for sample in samples if "t" in sample]
completion = result.get("completion_wall", float("inf"))
after = [s for s in samples if completion <= s["observed_wall"] <= completion + 2.5]
floor = manifest["initial_state"]["objects"][prop][2]


def raised(s):
    xyz = s["objects"][prop][:3]
    return xyz[2] >= floor + 0.03 and math.dist(xyz[:2], s["pose"][:2]) < 0.60 and s["joints"]["joint6"] > -0.065


stable = bool(after) and after[-1]["observed_wall"] - after[0]["observed_wall"] >= 2
stable = stable and all(raised(s) for s in after)
if stable:
    origin = after[0]["objects"][prop][:3]
    stable = all(math.dist(origin, s["objects"][prop][:3]) < 0.02 for s in after)
durability = [s for s in samples if completion <= s["observed_wall"] <= completion + 20.5]
durable = bool(durability) and durability[-1]["observed_wall"] - durability[0]["observed_wall"] >= 20
durable = durable and all(raised(s) for s in durability)
if durable:
    origin = durability[0]["objects"][prop][:3]
    durable = all(math.dist(origin, s["objects"][prop][:3]) < 0.02 for s in durability)
# The server intentionally finalizes a cancelled skill as a successful ROS
# goal. Its skill-level success_type, not ROS status alone, distinguishes it.
cancelled = result.get("success_type") == "cancelled" or result.get("cancel_wall") is not None
success = bool(result.get("success") and stable and durable and not result.get("timed_out") and not cancelled)
summary = {
    **result,
    "stable_hold": bool(stable),
    "durable_hold_20s": bool(durable),
    "success": success,
    "end_to_end_s": result.get("action_elapsed_s", 0) + 2 if success else None,
    "final_object": samples[-1]["objects"][prop],
    "final_j6": samples[-1]["joints"]["joint6"],
    "judge": "Action success, raised prop within0.60m, nonempty claw,2s stable initial hold; additional20s durability gate within2cm",
    "physics_to_wall_ratio": (samples[-1]["t"] - samples[0]["t"])
    / (samples[-1]["observed_wall"] - samples[0]["observed_wall"]),
}
if (a.trial / "events.json").exists():
    events = json.loads((a.trial / "events.json").read_text())
    summary["phases"] = [
        {k: v for k, v in e.items() if k in ("phase", "elapsed_s")} for e in events if e["kind"] == "phase_end"
    ]
    summary["provider_calls"] = sum(e["kind"] == "provider_start" for e in events)
    summary["provider_seconds"] = sum(e.get("elapsed_s", 0) for e in events if e["kind"] == "provider_end")
(a.trial / "judgement.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
