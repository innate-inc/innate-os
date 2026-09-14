#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
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


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [dict(row) for row in reader]


def _fmt_num(value: object, digits: int) -> str:
    num = _safe_float(value)
    if num is None:
        return "NA"
    return f"{num:.{digits}f}"


def _distance_bin_start(label: str) -> float:
    text = label.strip()
    if not text:
        return float("inf")
    if text.endswith("+"):
        return _safe_float(text[:-1]) or float("inf")
    if "-" in text:
        left, _, _ = text.partition("-")
        return _safe_float(left) or float("inf")
    return _safe_float(text) or float("inf")


def _write_regenerate_script(run_dir: Path, columns: int, no_lidar_panel: bool) -> Path:
    script_path = run_dir / "REGENERATE_ARTIFACTS.sh"
    extra = " --no-lidar-panel" if no_lidar_panel else ""
    content = "\n".join(
        [
            "#!/usr/bin/env bash",
            "set -euo pipefail",
            "",
            'ROOT="/home/jetson1/innate-os"',
            f'RUN_DIR="{run_dir}"',
            "",
            "source /opt/ros/humble/setup.zsh",
            'source "$ROOT/ros2_ws/install/setup.zsh"',
            'python3 "$ROOT/scripts/stereo_benchmark_postprocess.py" \\',
            '  --run-dir "$RUN_DIR" \\',
            f"  --columns {max(1, int(columns))}{extra}",
            "",
        ]
    )
    script_path.write_text(content, encoding="utf-8")
    os.chmod(script_path, 0o755)
    return script_path


def _write_markdown_report(
    run_dir: Path,
    summary_json: Path,
    rows: list[dict[str, object]],
    columns: int,
    no_lidar_panel: bool,
) -> Path:
    report_path = run_dir / "RESULTS_SUMMARY_ALL_MODELS.md"
    distance_rows = _load_csv_rows(run_dir / "all_models_distance_error_distribution.csv")
    heatmap_rows = _load_csv_rows(run_dir / "all_models_frame_region_error_heatmap.csv")
    regen_script = run_dir / "REGENERATE_ARTIFACTS.sh"

    lines: list[str] = []
    lines.append("# Stereo Benchmark Results (Text Summary)")
    lines.append("")
    lines.append(f"- Run folder: `{run_dir}`")
    lines.append(f"- Summary JSON: `{summary_json.name}`")
    lines.append(
        f"- Regenerate command: `python3 /home/jetson1/innate-os/scripts/stereo_benchmark_postprocess.py --run-dir {run_dir} --columns {max(1, int(columns))}{' --no-lidar-panel' if no_lidar_panel else ''}`"
    )
    lines.append("")
    lines.append("## Key Runtime Metrics")
    lines.append("")
    lines.append("| Model | Infer P95 ms | Infer P99 ms | FPS | Dropped % | RMSE m | MAE m | GPU P95 % | Power P95 W | Max Temp C |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("model", "")),
                    _fmt_num(row.get("inference_latency_p95_ms"), 1),
                    _fmt_num(row.get("inference_latency_p99_ms"), 1),
                    _fmt_num(row.get("inference_effective_fps"), 2),
                    _fmt_num(row.get("dropped_frames_pct"), 2),
                    _fmt_num(row.get("rmse_m"), 3),
                    _fmt_num(row.get("mae_m"), 3),
                    _fmt_num(row.get("gpu_util_p95_pct"), 1),
                    _fmt_num(row.get("power_p95_w"), 2),
                    _fmt_num(row.get("max_temperature_c"), 1),
                ]
            )
            + " |"
        )
    lines.append("")

    if distance_rows:
        lines.append("## Distance Error Bins")
        lines.append("")
        by_model: dict[str, list[dict[str, str]]] = {}
        for item in distance_rows:
            model = item.get("model", "")
            by_model.setdefault(model, []).append(item)
        for model in sorted(by_model):
            lines.append(f"### {model}")
            lines.append("")
            lines.append("| Distance Bin m | Count | Mean Abs Error m | P90 Abs Error m | P95 Abs Error m |")
            lines.append("|---|---:|---:|---:|---:|")
            model_rows = sorted(
                by_model[model],
                key=lambda r: _distance_bin_start(r.get("distance_bin_m", "")),
            )
            for item in model_rows:
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            item.get("distance_bin_m", ""),
                            str(int(float(item.get("count", "0") or "0"))),
                            _fmt_num(item.get("mean_abs_error_m"), 3),
                            _fmt_num(item.get("p90_abs_error_m"), 3),
                            _fmt_num(item.get("p95_abs_error_m"), 3),
                        ]
                    )
                    + " |"
                )
            lines.append("")

    if heatmap_rows:
        lines.append("## Worst Frame Regions (By Mean Error)")
        lines.append("")
        lines.append("| Model | Region (row,col) | Count | Mean Abs Error m | P95 Abs Error m |")
        lines.append("|---|---|---:|---:|---:|")
        grouped: dict[str, list[dict[str, str]]] = {}
        for item in heatmap_rows:
            grouped.setdefault(item.get("model", ""), []).append(item)
        for model in sorted(grouped):
            valid = []
            for item in grouped[model]:
                count_val = _safe_float(item.get("count"))
                mean_val = _safe_float(item.get("mean_abs_error_m"))
                if count_val is None or count_val <= 0 or mean_val is None:
                    continue
                valid.append(item)
            valid.sort(key=lambda item: _safe_float(item.get("mean_abs_error_m")) or -1.0, reverse=True)
            for item in valid[:3]:
                row_idx = int(float(item.get("row", "0") or "0")) + 1
                col_idx = int(float(item.get("col", "0") or "0")) + 1
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            model,
                            f"{row_idx},{col_idx}",
                            str(int(float(item.get("count", "0") or "0"))),
                            _fmt_num(item.get("mean_abs_error_m"), 3),
                            _fmt_num(item.get("p95_abs_error_m"), 3),
                        ]
                    )
                    + " |"
                )
        lines.append("")

    lines.append("## Commit Strategy")
    lines.append("")
    lines.append("- Commit text/CSV artifacts, not PNG/MP4 binaries.")
    lines.append("- Keep the raw run bag folders available locally to regenerate visuals later.")
    lines.append("- Run folders under recordings/ are gitignored in this repo.")
    lines.append("- Export this bundle with --export-text-dir, then commit the exported folder.")
    lines.append("- Use the helper script to regenerate all charts/overlays/videos in-place.")
    lines.append("")
    lines.append("```bash")
    lines.append(f"./{regen_script.name}")
    lines.append("```")
    lines.append("")
    lines.append("Suggested add list (from export folder):")
    lines.append("")
    lines.append("```bash")
    lines.append("git add <export_dir>/")
    lines.append("```")
    lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    return report_path


def _export_text_artifacts(run_dir: Path, summary_json: Path, export_root: Path) -> Path:
    export_dir = export_root.expanduser().resolve() / run_dir.name
    export_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []

    for path in sorted(run_dir.glob("stereo_depth_benchmark_*.json")):
        shutil.copy2(path, export_dir / path.name)
        copied.append(path.name)
    for path in sorted(run_dir.glob("stereo_depth_benchmark_*.csv")):
        shutil.copy2(path, export_dir / path.name)
        copied.append(path.name)

    fixed_names = [
        "all_models_distance_error_distribution.csv",
        "all_models_frame_region_error_heatmap.csv",
        "all_models_performance_overview.csv",
        "RESULTS_MANIFEST_ALL_MODELS.txt",
        "RESULTS_SUMMARY_ALL_MODELS.md",
        "REGENERATE_ARTIFACTS.sh",
    ]
    for name in fixed_names:
        src = run_dir / name
        if not src.exists():
            continue
        shutil.copy2(src, export_dir / src.name)
        copied.append(src.name)

    source_bag_dirs = [path for path in sorted(run_dir.iterdir()) if path.is_dir() and (path / "metadata.yaml").exists()]
    run_name = run_dir.name
    bag_stamp = None
    match = re.search(r"__bag_(\d{8}_\d{6})__", run_name)
    if match:
        bag_stamp = match.group(1)
    canonical_candidate = Path(f"/home/jetson1/innate-os/recordings/stereo_canonical_{bag_stamp}") if bag_stamp else None

    source_manifest = export_dir / "SOURCE_BAGS.txt"
    source_lines = [
        f"source_run_dir={run_dir}",
        f"summary_json={summary_json.name}",
    ]
    if canonical_candidate is not None:
        source_lines.append(f"canonical_bag={canonical_candidate}")
    for bag_dir in source_bag_dirs:
        source_lines.append(f"run_bag={bag_dir}")
    source_manifest.write_text("\n".join(source_lines) + "\n", encoding="utf-8")
    copied.append(source_manifest.name)

    readme = export_dir / "README.md"
    readme_lines = [
        "# Stereo Benchmark Text Export",
        "",
        f"- Source run folder: `{run_dir}`",
        f"- Preferred summary JSON: `{summary_json.name}`",
        "- This export intentionally excludes PNG/MP4 binaries.",
        "",
        "## Regenerate visual artifacts",
        "",
        "Run on the robot/workspace where the source run bag data exists:",
        "",
        "```bash",
        f"{run_dir}/REGENERATE_ARTIFACTS.sh",
        "```",
        "",
        "## Source bags for this run",
        "",
        "These are the rosbag directories backing this run's metrics/overlays:",
        "",
    ]
    if source_bag_dirs:
        for bag_dir in source_bag_dirs:
            readme_lines.append(f"- `{bag_dir}`")
    else:
        readme_lines.append("- None discovered under the run folder.")
    if canonical_candidate is not None:
        readme_lines.extend(
            [
                "",
                "Canonical input bag candidate:",
                "",
                f"- `{canonical_candidate}`",
            ]
        )
    readme_lines.extend(
        [
            "",
            "Bags are intentionally not committed here (they are too large).",
            "",
            "If you want a portable backup, sync them to external storage:",
            "",
            "```bash",
        ]
    )
    backup_root = "<backup_root>"
    if canonical_candidate is not None:
        readme_lines.append(
            f"rsync -a --info=progress2 {canonical_candidate} {backup_root}/{run_name}/"
        )
    for bag_dir in source_bag_dirs:
        readme_lines.append(f"rsync -a --info=progress2 {bag_dir} {backup_root}/{run_name}/")
    readme_lines.extend(
        [
            "```",
            "",
            "Use `SOURCE_BAGS.txt` in this folder as the authoritative source-bag pointer list.",
            "",
        "## Included files",
        "",
        ]
    )
    for name in sorted(set(copied)):
        readme_lines.append(f"- `{name}`")
    readme_lines.append("")
    readme.write_text("\n".join(readme_lines), encoding="utf-8")
    return export_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate all-model overlays, charts, and an N-panel video.")
    parser.add_argument("--run-dir", required=True, help="Benchmark run folder with model bags and summary JSON.")
    parser.add_argument("--summary-json", help="Optional summary JSON path. Defaults to newest in run dir.")
    parser.add_argument("--columns", type=int, default=3, help="N-panel grid columns.")
    parser.add_argument("--no-lidar-panel", action="store_true", help="Exclude lidar reference panel from N-panel.")
    parser.add_argument(
        "--export-text-dir",
        help="Optional tracked folder to copy text artifacts into (example: docs/stereo_benchmark_exports).",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Write markdown report + regenerate script only (no chart/video generation).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir).expanduser().resolve()
    summary_json = _resolve_summary(run_dir, args.summary_json)
    rows_raw = json.loads(summary_json.read_text())
    if not isinstance(rows_raw, list):
        raise RuntimeError(f"Expected summary JSON list: {summary_json}")
    rows = [row for row in rows_raw if isinstance(row, dict)]
    if not rows:
        raise RuntimeError(f"No summary rows found: {summary_json}")
    by_model = {str(row.get("model", "")): row for row in rows}

    if args.report_only:
        regenerate_script = _write_regenerate_script(
            run_dir=run_dir,
            columns=max(1, int(args.columns)),
            no_lidar_panel=bool(args.no_lidar_panel),
        )
        markdown_report = _write_markdown_report(
            run_dir=run_dir,
            summary_json=summary_json,
            rows=rows,
            columns=max(1, int(args.columns)),
            no_lidar_panel=bool(args.no_lidar_panel),
        )
        manifest = run_dir / "RESULTS_MANIFEST_ALL_MODELS.txt"
        lines = [
            f"run_dir={run_dir}",
            f"summary={summary_json.name}",
            "report_only=true",
            f"markdown_report={markdown_report.name}",
            f"regenerate_script={regenerate_script.name}",
        ]
        if args.export_text_dir:
            export_dir = _export_text_artifacts(
                run_dir=run_dir,
                summary_json=summary_json,
                export_root=Path(str(args.export_text_dir)),
            )
            lines.append(f"export_text_dir={export_dir}")
        manifest.write_text("\n".join(lines) + "\n")
        return 0

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
    regenerate_script = _write_regenerate_script(
        run_dir=run_dir,
        columns=max(1, int(args.columns)),
        no_lidar_panel=bool(args.no_lidar_panel),
    )
    markdown_report = _write_markdown_report(
        run_dir=run_dir,
        summary_json=summary_json,
        rows=rows,
        columns=max(1, int(args.columns)),
        no_lidar_panel=bool(args.no_lidar_panel),
    )
    lines.append(f"markdown_report={markdown_report.name}")
    lines.append(f"regenerate_script={regenerate_script.name}")
    if args.export_text_dir:
        export_dir = _export_text_artifacts(
            run_dir=run_dir,
            summary_json=summary_json,
            export_root=Path(str(args.export_text_dir)),
        )
        lines.append(f"export_text_dir={export_dir}")
    manifest.write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
