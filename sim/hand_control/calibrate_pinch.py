"""Pose-level kernel calibration. Requires NumPy from the simulator environment."""

import hashlib
import json
import sys
from pathlib import Path

import numpy as np

data_file, base_file, output = map(Path, sys.argv[1:])
data = json.loads(data_file.read_text())
rows = data["rows"]
profile = json.loads(base_file.read_text())
rotation_mode = profile.get("pinch", {}).get("rotation")


def kernel(a, b, w):
    return np.exp(-np.sum((a[:, None, :] - b[None, :, :]) ** 2, axis=2) / (2 * w * w))


def fit(key, open_only):
    usable = [r for r in rows if not open_only or r["grip"] > 0.1]
    X = np.array([np.median([s[key] for s in r["pinch"] if s["valid"]], axis=0) for r in usable])
    Y = np.array(
        [[0, 0, 0] if r["id"] in ["left", "right", "high", "low", "forward", "back"] else r["target"] for r in usable]
    )
    ids = [i for i, r in enumerate(usable) if r["role"] != "repeat_check"]
    weights = np.array([2 if usable[i]["group"] == "refine" else 1 for i in ids])
    best = None
    for width in [0.5, 0.8, 1.2, 1.8, 2.8]:
        K = kernel(X[ids], X[ids], width)
        for lam in [0.001, 0.01, 0.05, 0.2, 1.0]:
            inv = np.linalg.inv(K + np.diag(lam / weights))
            c = inv @ Y[ids]
            loo = c / np.diag(inv)[:, None]
            score = float(np.sqrt(np.mean(loo**2)))
            if best is None or score < best[0]:
                best = score, width, lam, c
    score, width, lam, c = best
    report = {"leavePoseOutRmsDegrees": score * 180 / np.pi, "width": width, "ridge": lam, "heldOut": []}
    for i, r in enumerate(usable):
        if r["role"] == "repeat_check":
            pred = kernel(X[i : i + 1], X[ids], width) @ c
            report["heldOut"].append(
                {"id": r["id"], "prediction": (pred[0] * 180 / np.pi).tolist(), "target": (Y[i] * 180 / np.pi).tolist()}
            )
    return {"width": width, "centers": X[ids].tolist(), "coefficients": c.tolist()}, report


orientation, r1 = fit("features", True)
stable, r2 = fit("stableFeatures", False)
profile["pinch"] = {
    "version": 1,
    "session": data["session"],
    "source_hash": data["source_hash"],
    "pose_count": len(rows),
    "orientation": orientation,
    "stable": stable,
    "positionGain": [0.08, 0.4, 0.4],
    "grip": {"closed": 0.18, "open": 1.0, "visibleClosed": 0.32},
}
profile["workspace"]["pinch_point"] = True
if rotation_mode == "direct":
    # Re-fitting the stored fallback must not disable the operator's direct
    # rotation preference or mismatch the simulator's wider joint range.
    profile["pinch"]["rotation"] = "direct"
    profile["workspace"]["direct_rotation"] = True
profile["revision"] = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
output.write_text(json.dumps(profile, indent=2) + "\n")
report = {
    "orientation": r1,
    "stable": r2,
    "repeatPolicy": "Every repeat is excluded from fitting and parameter selection.",
}
output.with_suffix(".report.json").write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
