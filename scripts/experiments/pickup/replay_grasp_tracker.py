#!/usr/bin/env python3
"""Replay an exported pickup diagnostic run without model calls or robot motion.

Pass the directory containing events.jsonl and its JPEGs. Intermediate motion
frames in the diagnostic recorder were JPEG re-encoded; replay is approximate,
so use independent image correspondence to audit anchor drift as well.
"""

import argparse
import importlib.util
import json
from pathlib import Path

import cv2


def replay(directory):
    source = Path(__file__).resolve().parents[3] / "workspace/innate_skills/grasp_tracker.py"
    spec = importlib.util.spec_from_file_location("replay_grasp_tracker_impl", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    results = []
    raw = None
    for event in events:
        kind = event["kind"]
        if kind == "model_output" and event["view"] == "wrist":
            plans = event["result"]["detections"]
        elif kind == "tracker_seed":
            # Reproduce the selected instance rather than assuming the first
            # detection when the model returned multiple objects.
            plan = min(
                plans,
                key=lambda p: (
                    (p["grasp_point_2d"][1] * 0.64 - event["guess"][0]) ** 2
                    + (p["grasp_point_2d"][0] * 0.48 - event["guess"][1]) ** 2
                ),
            )
            y0, x0, y1, x1 = plan["box_2d"]
            box = (int(x0 * 0.64), int(y0 * 0.48), int((x1 - x0) * 0.64), int((y1 - y0) * 0.48))
            image = cv2.imread(str(directory / event["frame"]))
            tracker = module.GraspPointTracker(cv2.cvtColor(image, cv2.COLOR_BGR2HSV), box, event["guess"])
            raw = None
            results.append({"seed": event["frame"], "seed_ok": bool(tracker.ok), "frames": []})
        elif kind == "wrist_frame":
            raw = event["frame"] if event["decoded"] else None
        elif kind == "track":
            image = cv2.imread(str(directory / (raw or event["frame"])))
            point = tracker.update(cv2.cvtColor(image, cv2.COLOR_BGR2HSV))
            raw = None
            results[-1]["frames"].append(
                dict(frame=event["frame"], point=point, reason=tracker.reason, features=len(tracker.points))
            )
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.directory), indent=2))
