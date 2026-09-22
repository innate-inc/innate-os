#!/usr/bin/env python3
"""Render replay plots and overlays after timing runs have finished."""

import argparse
import json
import subprocess
from pathlib import Path

import cv2
import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from analyze import signal
from benchmark import ROOT, sha, write_json

NAMES = {"mediapipe": "MediaPipe", "rtmpose": "RTMDet + RTMPose"}
COLORS = {"mediapipe": "#087e74", "rtmpose": "#bc5a21"}
FINGERS = [[0, 1, 2, 3, 4], [0, 5, 6, 7, 8], [0, 9, 10, 11, 12], [0, 13, 14, 15, 16], [0, 17, 18, 19, 20]]


def plots(metrics, runs, conditions, output):
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 11,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.titleweight": "bold",
        }
    )
    fig, ax = plt.subplots(figsize=(10, 4.8), layout="constrained")
    y, labels = 0, []
    for model in NAMES:
        for metric in ["latency_ms_p50", "latency_ms_p95"]:
            values = np.array([r["summary"]["overall"][metric] for r in metrics["runs"] if r["model"] == model])
            median = float(np.median(values))
            ax.barh(y, median, color=COLORS[model], height=0.65, alpha=1 if "p50" in metric else 0.65)
            ax.errorbar(
                median,
                y,
                xerr=[[median - values.min()], [values.max() - median]],
                color="#202b36",
                capsize=5,
                fmt="none",
                linewidth=1.5,
            )
            ax.text(values.max() + 2, y, f"{median:.1f} ms", va="center")
            labels.append(f"{NAMES[model]} · {metric[-3:]}")
            y += 1
    ax.set_yticks(range(4), labels)
    ax.invert_yaxis()
    ax.axvline(1000 / 30, color="#718096", linestyle="--", linewidth=1, label="33.3 ms / frame at 30 Hz")
    ax.set_xlim(0, max(r["summary"]["overall"]["latency_ms_p95"] for r in metrics["runs"]) * 1.3)
    ax.set_xlabel("Synchronous pipeline time (ms) · lower is better")
    ax.set_title("MediaPipe is faster on this Mac M1 Pro", loc="left", pad=16)
    ax.legend(loc="lower right", frameon=False)
    fig.suptitle(
        "2,877 frames per replay · median of three runs; whiskers show run range\nCPU pipelines; camera, decode, display, network and robot excluded",
        fontsize=10,
        color="#526071",
        y=1.1,
    )
    fig.savefig(output / "latency.png", dpi=160, bbox_inches="tight")
    plt.close(fig)

    fig, axes = plt.subplots(2, 3, figsize=(12, 7), layout="constrained")
    for ax, (condition, channel) in zip(
        axes.flat,
        [("left", "x"), ("right", "x"), ("toward", "palm_scale"), ("up", "y"), ("down", "y"), ("away", "palm_scale")],
        strict=True,
    ):
        for model, rows in runs.items():
            selected = [r for r in rows if conditions[r["clip_id"]] == condition]
            t = np.array([r["pts_ms"] / 1000 for r in selected])
            values = np.array([s[channel] if (s := signal(r)) else np.nan for r in selected])
            baseline = np.nanmedian(values[(t >= 0.25) & (t <= 1)])
            ax.plot(t, values - baseline, color=COLORS[model], linewidth=1.5, label=NAMES[model])
        ax.axhline(0, color="#a0aab5", linewidth=0.7)
        ax.set_title(condition.capitalize(), loc="left")
        ax.set_xlabel("Video time (s)")
        ax.set_ylabel("Palm size change (px)" if channel == "palm_scale" else f"Image {channel} change (px)")
        ax.grid(alpha=0.15)
    axes[0, 0].legend(frameon=False, fontsize=9)
    fig.suptitle(
        "Both pipelines follow all six net movement directions\nRaw signals from replay 1; camera coordinates are unmirrored. Palm size is not metric depth.",
        fontsize=14,
    )
    fig.savefig(output / "motion.png", dpi=160, bbox_inches="tight")
    plt.close(fig)


def overlay(frame, row):
    panel = frame.copy()
    for hand in row["hands"]:
        points = np.clip(np.round(hand["xy"]), -10000, 10000).astype(int)
        for finger in FINGERS:
            for a, b in zip(finger, finger[1:], strict=False):
                cv2.line(panel, tuple(points[a]), tuple(points[b]), (40, 230, 220), 2, cv2.LINE_AA)
        for point in points:
            cv2.circle(panel, tuple(point), 3, (250, 100, 50), -1, cv2.LINE_AA)
    center = signal(row)
    if center:
        point = (round(center["x"]), round(center["y"]))
        cv2.drawMarker(panel, point, (255, 255, 255), cv2.MARKER_CROSS, 18, 2)
    return panel


def video(manifest_path, manifest, runs, output):
    indexed = {model: {(r["clip_id"], r["frame"]): r for r in rows} for model, rows in runs.items()}
    temporary = output / "comparison-raw.avi"
    writer = cv2.VideoWriter(str(temporary), cv2.VideoWriter_fourcc(*"MJPG"), 30, (1280, 576))
    if not writer.isOpened():
        raise ValueError("Cannot create overlay video")
    chapters, total = [], 0
    try:
        for condition in ("reenter", "occlusion", "fast"):
            clip = next(c for c in manifest["clips"] if c["clip"] == condition)
            chapters.append({"condition": condition, "start_seconds": total / 30})
            cap = cv2.VideoCapture(str(manifest_path.parent / clip["video"]))
            n = 0
            try:
                while True:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    canvas = np.full((576, 1280, 3), (28, 23, 20), np.uint8)
                    for i, model in enumerate(NAMES):
                        row = indexed[model][(clip["id"], n)]
                        if abs(cap.get(cv2.CAP_PROP_POS_MSEC) - row["pts_ms"]) > 0.01:
                            raise ValueError("Overlay timestamp mismatch")
                        canvas[56:536, i * 640 : (i + 1) * 640] = overlay(frame, row)
                        text = f"{NAMES[model]} | {condition} | {row['pts_ms'] / 1000:.2f}s"
                        cv2.putText(
                            canvas,
                            text,
                            (i * 640 + 14, 34),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.7,
                            (245, 245, 245),
                            2,
                            cv2.LINE_AA,
                        )
                    cv2.putText(
                        canvas,
                        "Offline replay at ~source speed | all detections shown | white cross: largest-hand palm center",
                        (14, 563),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.57,
                        (230, 225, 218),
                        1,
                        cv2.LINE_AA,
                    )
                    writer.write(canvas)
                    if (condition == "occlusion" and n == 195) or (condition == "reenter" and n == 209):
                        cv2.imwrite(str(output / f"{condition}.jpg"), canvas)
                    n += 1
                    total += 1
            finally:
                cap.release()
            expected = sum(r["clip_id"] == clip["id"] for r in runs["mediapipe"])
            if n != expected:
                raise ValueError("Overlay decode did not match inference")
    finally:
        writer.release()
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-n",
            "-i",
            str(temporary),
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(output / "comparison.mp4"),
        ],
        check=True,
    )
    temporary.unlink()
    write_json(
        output / "video.json",
        {
            "frames": total,
            "fps": 30,
            "chapters": chapters,
            "note": "Constant 30 fps presentation, approximately source speed. This is not live latency.",
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=ROOT / "data/manifest.json")
    args = parser.parse_args()
    metrics = json.loads(args.metrics.read_text())
    if sha(args.manifest) != metrics["manifest_sha256"]:
        raise ValueError("Wrong manifest")
    manifest = json.loads(args.manifest.read_text())
    runs = {}
    for model in NAMES:
        run = next(r for r in metrics["runs"] if r["model"] == model)
        path = args.metrics.parent.parent / run["run"] / "predictions.jsonl"
        if sha(path) != run["predictions_sha256"]:
            raise ValueError("Predictions changed")
        runs[model] = [json.loads(line) for line in path.read_text().splitlines()]
    output = args.metrics.parent / "figures"
    output.mkdir(exist_ok=False)
    plots(metrics, runs, {c["id"]: c["clip"] for c in manifest["clips"]}, output)
    video(args.manifest, manifest, runs, output)
    write_json(
        output / "provenance.json",
        {
            "renderer_sha256": sha(__file__),
            "metrics_sha256": sha(args.metrics),
            "matplotlib": matplotlib.__version__,
            "opencv": cv2.__version__,
        },
    )
    print(output)


if __name__ == "__main__":
    main()
