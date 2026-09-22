#!/usr/bin/env python3
"""Score frozen, visually reviewed calibration samples and replay diagnostics.

These coarse boxes do not measure fingertip accuracy or camera-relative depth.
The analysis does not modify predictions, labels, or model configurations.
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

import numpy as np
from benchmark import ROOT, sha, write_json

PALM = [0, 5, 9, 13, 17]


def read_frozen(path):
    if sha(path) != path.with_suffix(".sha256").read_text().strip():
        raise ValueError(f"Reference changed: {path}")
    return json.loads(path.read_text())


def envelope(hand, width, height):
    xy = np.asarray(hand["xy"], dtype=float)
    if xy.shape != (21, 2) or not np.isfinite(xy).all():
        raise ValueError("Expected 21 finite 2D landmarks")
    xy = np.clip(xy, [0, 0], [width, height])
    return np.concatenate((xy.min(axis=0), xy.max(axis=0))).tolist()


def area(box):
    return max(0, box[2] - box[0]) * max(0, box[3] - box[1])


def iou(a, b):
    intersection = area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])
    union = area(a) + area(b) - intersection
    return intersection / union if union > 0 else 0.0


def signal(row):
    """Largest visible landmark envelope; no truth is used to select a hand."""
    if not row["hands"]:
        return None
    hand = max(row["hands"], key=lambda h: area(envelope(h, row["width"], row["height"])))
    xy = np.asarray(hand["xy"])
    center = xy[PALM].mean(axis=0)
    scale = np.sqrt((np.sum((xy[0] - xy[9]) ** 2) + np.sum((xy[5] - xy[17]) ** 2)) / 2)
    return {"x": float(center[0]), "y": float(center[1]), "palm_scale": float(scale)}


def score_samples(rows, labels):
    indexed = {(r["clip_id"], r["frame"]): r for r in rows}
    if len(indexed) != len(rows):
        raise ValueError("Duplicate prediction frame")
    frames, counts = [], dict(tp=0, fn=0, tn=0, fp=0)
    for label in labels:
        key = (label["clip_id"], label["frame"])
        if key not in indexed:
            raise ValueError(f"Missing annotated prediction: {key}")
        row = indexed[key]
        if abs(row["pts_ms"] - label["pts_ms"]) > 0.01:
            raise ValueError(f"Reference timestamp mismatch: {key}")
        present = bool(row["hands"])
        counts[("tp" if present else "fn") if label["present"] else ("fp" if present else "tn")] += 1
        overlaps = []
        scorable = label["present"] and not label["identity_ambiguous"]
        if scorable:
            overlaps = [iou(envelope(h, row["width"], row["height"]), label["bbox_xyxy"]) for h in row["hands"]]
        frames.append(
            {
                "clip_id": label["clip_id"],
                "frame": label["frame"],
                "condition": label["condition"],
                "reference_present": label["present"],
                "predicted_present": present,
                "localization_scorable": scorable,
                "best_iou": max(overlaps, default=0) if scorable else None,
            }
        )
    spatial = [r["best_iou"] for r in frames if r["localization_scorable"]]
    return {
        "sampled_presence": counts,
        "target_localization": {
            "denominator": len(spatial),
            "iou_ge_0_3": sum(x >= 0.3 for x in spatial),
            "iou_ge_0_5": sum(x >= 0.5 for x in spatial),
            "median_iou": float(np.median(spatial)) if spatial else None,
        },
        "frames": frames,
    }


def direction_check(rows, channel, expected_sign, windows):
    values = []
    for start, end in windows:
        eligible = [row for row in rows if start <= row["pts_ms"] <= end]
        observed = [s[channel] for row in eligible if (s := signal(row)) is not None]
        # Both windows need at least half their frames; do not silently score a
        # direction from a single lucky detection or a missing window.
        if len(observed) < 3 or len(observed) < len(eligible) / 2:
            return {"status": "abstain", "reason": "insufficient observations"}
        values.append(float(np.median(observed)))
    delta = values[1] - values[0]
    threshold = max(1.0, abs(values[0]) * 0.01) if channel == "palm_scale" else 5.0
    status = "abstain" if abs(delta) < threshold else ("correct" if delta * expected_sign > 0 else "wrong")
    return {
        "status": status,
        "start_median_px": values[0],
        "end_median_px": values[1],
        "delta_px": delta,
        "abstention_threshold_px": threshold,
    }


def diagnostics(rows, conditions, reference):
    by_condition = defaultdict(list)
    for row in rows:
        by_condition[conditions[row["clip_id"]]].append(row)
    directions = {
        name: direction_check(
            by_condition[name], value["channel"], value["expected_sign"], reference["direction_windows_ms"]
        )
        for name, value in reference["direction_reference"].items()
    }
    entries = []
    events = reference["entry_events"]
    for i, event in enumerate(events):
        end = events[i + 1]["first_any_hand_frame"] if i + 1 < len(events) else float("inf")
        found = next(
            (r for r in by_condition["reenter"] if event["first_any_hand_frame"] <= r["frame"] < end and r["hands"]),
            None,
        )
        entries.append(
            dict(
                event,
                first_detected_frame=found["frame"] if found else None,
                first_detected_pts_ms=found["pts_ms"] if found else None,
                delay_from_any_visibility_ms=found["pts_ms"] - event["first_any_hand_pts_ms"] if found else None,
                delay_from_usable_palm_ms=found["pts_ms"] - event["clearly_usable_open_palm_pts_ms"] if found else None,
            )
        )
    still = by_condition["hold"]
    moves = []
    for a, b in zip(still, still[1:], strict=False):
        sa, sb = signal(a), signal(b)
        if sa and sb:
            moves.append(np.hypot(sb["x"] - sa["x"], sb["y"] - sa["y"]))
    scales = [s["palm_scale"] for row in still if (s := signal(row)) is not None]
    return {
        "net_direction_checks": directions,
        "reentry_events": entries,
        "hold_output_motion_not_estimator_error": {
            "consecutive_observed_pairs": len(moves),
            "palm_step_px_p50": float(np.median(moves)) if moves else None,
            "palm_step_px_p95": float(np.percentile(moves, 95)) if moves else None,
            "palm_scale_cv": float(np.std(scales) / np.mean(scales)) if scales else None,
        },
        "empty_clip_detections_to_review": [
            {"frame": r["frame"], "pts_ms": r["pts_ms"], "hands": r["hands"]}
            for r in by_condition["empty"]
            if r["hands"]
        ],
    }


def load_run(folder, manifest_path, manifest):
    config = json.loads((folder / "config.json").read_text())
    if config["status"] != "complete" or config["manifest_sha256"] != sha(manifest_path):
        raise ValueError(f"Incomplete run or different inputs: {folder}")
    rows = [json.loads(line) for line in (folder / "predictions.jsonl").read_text().splitlines()]
    clips = {c["id"] for c in manifest["clips"] if c["split"] == config["split"]}
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["clip_id"]].append(row)
    if set(grouped) != clips:
        raise ValueError(f"Missing/extra clips in {folder}")
    for clip_rows in grouped.values():
        if [r["frame"] for r in clip_rows] != list(range(len(clip_rows))):
            raise ValueError(f"Missing/duplicate/out-of-order frames in {folder}")
        if any(b["pts_ms"] < a["pts_ms"] for a, b in zip(clip_rows, clip_rows[1:], strict=False)):
            raise ValueError(f"Decreasing timestamps in {folder}")
    return config, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "data/manifest.json")
    parser.add_argument("--review", type=Path, default=ROOT / "data/review")
    parser.add_argument("--runs", type=Path, nargs="+", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    annotations = read_frozen(args.review / "annotations.json")
    temporal = read_frozen(args.review / "temporal_reference.json")
    for label in annotations["frames"]:
        if sha(args.review / label["image"]) != label["image_sha256"]:
            raise ValueError("Reviewed image changed")
    for clip in manifest["clips"]:
        for field, digest in (("video", "sha256"), ("metadata", "metadata_sha256")):
            if sha(args.manifest.parent / clip[field]) != clip[digest]:
                raise ValueError(f"Frozen input changed: {clip[field]}")
    conditions = {c["id"]: c["clip"] for c in manifest["clips"]}
    output = {
        "status": "calibration pilot; no held-out validation",
        "human_verified": annotations["human_verified"],
        "reference_sha256": sha(args.review / "annotations.json"),
        "temporal_reference_sha256": sha(args.review / "temporal_reference.json"),
        "manifest_sha256": sha(args.manifest),
        "scorer_sha256": sha(__file__),
        "signal_policy": "Largest visible landmark envelope; center=mean(0,5,9,13,17); palm_scale=sqrt((|p0-p9|^2+|p5-p17|^2)/2); source pixels, no smoothing",
        "direction_threshold_policy": "5 px for x/y, max(1 px, 1% initial scale) for size; diagnostic defaults, not fitted",
        "runs": [],
    }
    frame_signature, harness = None, None
    for folder in args.runs:
        config, rows = load_run(folder, args.manifest, manifest)
        signature = [(r["clip_id"], r["frame"], r["pts_ms"], r["width"], r["height"]) for r in rows]
        if frame_signature is not None and signature != frame_signature:
            raise ValueError("Runs do not cover identical frames")
        if harness is not None and config["harness_sha256"] != harness:
            raise ValueError("Harness changed between compared runs")
        frame_signature, harness = signature, config["harness_sha256"]
        result = {
            "run": folder.name,
            "model": config["model"],
            "config_sha256": sha(folder / "config.json"),
            "predictions_sha256": sha(folder / "predictions.jsonl"),
            "summary": json.loads((folder / "summary.json").read_text()),
            "sample_scores": score_samples(rows, annotations["frames"]),
            "diagnostics": diagnostics(rows, conditions, temporal),
        }
        output["runs"].append(result)
    args.output.mkdir(parents=True, exist_ok=False)
    write_json(args.output / "metrics.json", output)
    print(args.output / "metrics.json")


if __name__ == "__main__":
    main()
