#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
from math import isnan
from pathlib import Path

import cv2
import numpy as np


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True)


def _run_in_ros_env(command: str) -> None:
    setup = (
        "source /opt/ros/humble/setup.zsh && "
        "source /home/jetson1/innate-os/ros2_ws/install/setup.zsh && "
    )
    _run(["zsh", "-lc", f"{setup}{command}"])


def _resolve_summary(run_dir: Path, summary_json: str | None) -> Path:
    if summary_json:
        path = Path(summary_json).expanduser().resolve()
        if not path.exists():
            raise RuntimeError(f"Summary JSON not found: {path}")
        return path
    candidates = sorted(run_dir.glob("stereo_depth_benchmark_*.json"))
    if not candidates:
        raise RuntimeError(f"No summary JSON found in {run_dir}")
    return candidates[-1]


def _annotate_model(
    run_bag: str,
    lidar_topic: str,
    camera_info_topic: str,
    depth_topic: str | None,
    out_video: Path,
    out_image: Path,
) -> None:
    parts = [
        "ros2 run mars_cam stereo_depth_benchmark annotate",
        f"--bag {run_bag}",
        "--image-topic /mars/main_camera/left/image_raw",
        f"--lidar-topic {lidar_topic}",
        f"--camera-info-topic {camera_info_topic}",
    ]
    if depth_topic:
        parts.append(f"--depth-topic {depth_topic}")
        parts.append("--error-max-m 1.0")
    parts.extend(
        [
            f"--output-video {out_video}",
            f"--output-image {out_image}",
            "--image-frame-index 150",
        ]
    )
    _run_in_ros_env(" ".join(parts))


def _label_for_model(model_name: str) -> str:
    if model_name == "classical_stereo":
        return "classical"
    return model_name


def _compose_npanel(
    panel_videos: list[tuple[str, Path]],
    out_video: Path,
    out_preview: Path,
    columns: int,
) -> None:
    caps = [cv2.VideoCapture(str(path)) for _, path in panel_videos]
    if not all(cap.isOpened() for cap in caps):
        for cap in caps:
            cap.release()
        raise RuntimeError("Failed to open one or more panel videos.")

    fps = caps[0].get(cv2.CAP_PROP_FPS) or 10.0
    widths = [int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) for cap in caps]
    heights = [int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) for cap in caps]
    frames_total = min(int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) for cap in caps)
    target_h = min(heights)
    resized_w = [max(1, int(w * (target_h / h))) if h else w for w, h in zip(widths, heights)]

    panel_count = len(panel_videos)
    cols = max(1, min(columns, panel_count))
    rows = math.ceil(panel_count / cols)
    cell_w = max(resized_w)
    cell_h = target_h
    out_w = cell_w * cols
    out_h = cell_h * rows

    writer = cv2.VideoWriter(str(out_video), cv2.VideoWriter_fourcc(*"mp4v"), fps, (out_w, out_h))
    if not writer.isOpened():
        for cap in caps:
            cap.release()
        raise RuntimeError(f"Failed creating output video: {out_video}")

    preview_idx = min(149, max(frames_total - 1, 0))
    labels = [label for label, _ in panel_videos]

    for idx in range(frames_total):
        frames: list[np.ndarray] = []
        for cap, rw, label in zip(caps, resized_w, labels):
            ret, frame = cap.read()
            if not ret:
                frames = []
                break
            resized = cv2.resize(frame, (rw, target_h), interpolation=cv2.INTER_AREA)
            canvas = np.zeros((cell_h, cell_w, 3), dtype=np.uint8)
            xoff = (cell_w - rw) // 2
            canvas[:, xoff : xoff + rw] = resized
            cv2.rectangle(canvas, (0, 0), (min(cell_w - 1, 520), 36), (0, 0, 0), -1)
            cv2.putText(canvas, label, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
            frames.append(canvas)
        if len(frames) != panel_count:
            break

        while len(frames) < rows * cols:
            frames.append(np.zeros((cell_h, cell_w, 3), dtype=np.uint8))
        row_imgs = []
        for row_idx in range(rows):
            start = row_idx * cols
            row_imgs.append(np.hstack(frames[start : start + cols]))
        panel = np.vstack(row_imgs)
        writer.write(panel)
        if idx == preview_idx:
            cv2.imwrite(str(out_preview), panel)

    writer.release()
    for cap in caps:
        cap.release()


def _build_all_model_charts(summary_json: Path, out_dir: Path) -> None:
    _run_in_ros_env(
        " ".join(
            [
                "ros2 run mars_cam stereo_depth_benchmark charts",
                f"--summary-json {summary_json}",
                f"--output-dir {out_dir}",
                "--grid-rows 3",
                "--grid-cols 4",
            ]
        )
    )
    stem = summary_json.stem
    mapping = {
        f"{stem}_distance_error_distribution.png": "all_models_distance_error_distribution.png",
        f"{stem}_distance_error_distribution.csv": "all_models_distance_error_distribution.csv",
        f"{stem}_frame_region_error_heatmap.png": "all_models_frame_region_error_heatmap.png",
        f"{stem}_frame_region_error_heatmap.csv": "all_models_frame_region_error_heatmap.csv",
    }
    for src_name, dst_name in mapping.items():
        src = out_dir / src_name
        if src.exists():
            shutil.copy2(src, out_dir / dst_name)


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if isnan(num):
        return None
    return num


def _build_performance_overview(rows: list[dict[str, object]], out_dir: Path, prefix: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    metrics: list[tuple[str, str]] = [
        ("Inference FPS", "inference_effective_fps"),
        ("Inference P95 (ms)", "inference_latency_p95_ms"),
        ("Inference P99 (ms)", "inference_latency_p99_ms"),
        ("Cam->Disp P95 (ms)", "camera_to_disparity_latency_p95_ms"),
        ("Cam->PointCloud P95 (ms)", "camera_to_pointcloud_latency_p95_ms"),
        ("Dropped Frames (%)", "dropped_frames_pct"),
        ("CPU P95 (%)", "cpu_util_p95_pct"),
        ("GPU P95 (%)", "gpu_util_p95_pct"),
        ("RAM P95 (MB)", "ram_used_p95_mb"),
        ("Power P95 (W)", "power_p95_w"),
        ("Max Temp (C)", "max_temperature_c"),
        ("Valid Pairs", "valid_pairs"),
    ]
    csv_out = out_dir / f"{prefix}_performance_overview.csv"
    png_out = out_dir / f"{prefix}_performance_overview.png"

    with csv_out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["model"] + [key for _, key in metrics])
        writer.writeheader()
        for row in rows:
            out: dict[str, object] = {"model": str(row.get("model", ""))}
            for _, key in metrics:
                val = _safe_float(row.get(key))
                out[key] = "" if val is None else f"{val:.6g}"
            writer.writerow(out)

    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
    fig, axes = plt.subplots(3, 4, figsize=(20, 12), squeeze=False)
    for idx, (label, key) in enumerate(metrics):
        r = idx // 4
        c = idx % 4
        ax = axes[r][c]
        values: list[float] = []
        labels: list[str] = []
        bar_colors: list[str] = []
        for model_idx, row in enumerate(rows):
            val = _safe_float(row.get(key))
            if val is None:
                continue
            values.append(val)
            labels.append(str(row.get("model", f"m{model_idx}")))
            bar_colors.append(colors[model_idx % len(colors)])

        if not values:
            ax.set_title(label)
            ax.text(0.5, 0.5, "N/A", ha="center", va="center", transform=ax.transAxes)
            ax.set_xticks([])
            ax.set_yticks([])
            continue

        x = np.arange(len(values))
        bars = ax.bar(x, values, color=bar_colors, alpha=0.8)
        ax.set_title(label)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=20, ha="right")
        ax.grid(axis="y", alpha=0.3)
        ymax = max(values)
        pad = ymax * 0.04 if ymax > 0 else 0.1
        for bar, value in zip(bars, values):
            ax.text(
                bar.get_x() + bar.get_width() / 2.0,
                bar.get_height() + pad,
                f"{value:.2f}",
                ha="center",
                va="bottom",
                fontsize=8,
            )

    fig.suptitle("Stereo Benchmark Performance Overview", fontsize=16)
    fig.tight_layout()
    fig.savefig(png_out, dpi=180)
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate all-model overlays, charts, and an N-panel video.")
    parser.add_argument("--run-dir", required=True, help="Benchmark run folder with model bags and summary JSON.")
    parser.add_argument("--summary-json", help="Optional summary JSON path. Defaults to newest in run dir.")
    parser.add_argument("--columns", type=int, default=3, help="N-panel grid columns.")
    parser.add_argument("--no-lidar-panel", action="store_true", help="Exclude lidar reference panel from N-panel.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir).expanduser().resolve()
    summary_json = _resolve_summary(run_dir, args.summary_json)
    rows = json.loads(summary_json.read_text())
    by_model = {row["model"]: row for row in rows}
    if "classical_stereo" not in by_model:
        raise RuntimeError("summary JSON must include classical_stereo")

    classical = by_model["classical_stereo"]
    lidar_topic = str(classical.get("lidar_topic", "/scan"))
    camera_info_topic = str(classical.get("camera_info_topic", "/mars/main_camera/left/camera_info"))

    _build_all_model_charts(summary_json, run_dir)
    _build_performance_overview(rows, run_dir, prefix="all_models")

    panel_videos: list[tuple[str, Path]] = []
    if not args.no_lidar_panel:
        lidar_video = run_dir / "lidar_reference_all_models.mp4"
        lidar_image = run_dir / "lidar_reference_all_models.png"
        _annotate_model(
            run_bag=classical["run_bag"],
            lidar_topic=lidar_topic,
            camera_info_topic=camera_info_topic,
            depth_topic=None,
            out_video=lidar_video,
            out_image=lidar_image,
        )
        panel_videos.append(("lidar reference", lidar_video))

    for row in rows:
        model_name = str(row["model"])
        model_video = run_dir / f"{model_name}_lidar_vs_model.mp4"
        model_image = run_dir / f"{model_name}_lidar_vs_model.png"
        _annotate_model(
            run_bag=row["run_bag"],
            lidar_topic=str(row.get("lidar_topic", lidar_topic)),
            camera_info_topic=str(row.get("camera_info_topic", camera_info_topic)),
            depth_topic=str(row["depth_topic"]),
            out_video=model_video,
            out_image=model_image,
        )
        panel_videos.append((_label_for_model(model_name), model_video))

    out_video = run_dir / "all_models_npanel.mp4"
    out_preview = run_dir / "all_models_npanel_preview.png"
    _compose_npanel(
        panel_videos=panel_videos,
        out_video=out_video,
        out_preview=out_preview,
        columns=max(1, int(args.columns)),
    )

    manifest = run_dir / "RESULTS_MANIFEST_ALL_MODELS.txt"
    lines = [
        f"run_dir={run_dir}",
        f"summary={summary_json.name}",
        f"n_panel_video={out_video.name}",
        f"n_panel_preview={out_preview.name}",
        "chart_distance=all_models_distance_error_distribution.png",
        "chart_frame_region=all_models_frame_region_error_heatmap.png",
        "perf_chart=all_models_performance_overview.png",
        "perf_csv=all_models_performance_overview.csv",
        "panels:",
    ]
    for label, path in panel_videos:
        lines.append(f"- {label}: {path.name}")
    manifest.write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
