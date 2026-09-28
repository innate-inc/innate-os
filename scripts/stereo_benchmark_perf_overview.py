#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import json
from math import isnan
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt


METRICS: list[tuple[str, str]] = [
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


def _write_csv(rows: list[dict[str, object]], out_csv: Path) -> None:
    fields = ["model"] + [key for _, key in METRICS]
    with out_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            out: dict[str, object] = {"model": row.get("model", "")}
            for _, key in METRICS:
                val = _safe_float(row.get(key))
                out[key] = "" if val is None else f"{val:.6g}"
            writer.writerow(out)


def _plot(rows: list[dict[str, object]], out_png: Path) -> None:
    models = [str(row.get("model", "unknown")) for row in rows]
    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
    model_colors = [colors[idx % len(colors)] for idx in range(len(models))]

    fig, axes = plt.subplots(3, 4, figsize=(20, 12), squeeze=False)
    for idx, (label, key) in enumerate(METRICS):
        r = idx // 4
        c = idx % 4
        ax = axes[r][c]

        values: list[float] = []
        valid_models: list[str] = []
        valid_colors: list[str] = []
        for model_idx, row in enumerate(rows):
            val = _safe_float(row.get(key))
            if val is None:
                continue
            values.append(val)
            valid_models.append(str(row.get("model", f"m{model_idx}")))
            valid_colors.append(model_colors[model_idx])

        if not values:
            ax.set_title(label)
            ax.text(0.5, 0.5, "N/A", ha="center", va="center", transform=ax.transAxes)
            ax.set_xticks([])
            ax.set_yticks([])
            continue

        x = np.arange(len(values))
        bars = ax.bar(x, values, color=valid_colors, alpha=0.8)
        ax.set_title(label)
        ax.set_xticks(x)
        ax.set_xticklabels(valid_models, rotation=20, ha="right")
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
    fig.savefig(out_png, dpi=180)
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate performance overview chart from benchmark summary JSON.")
    parser.add_argument("--summary-json", required=True, help="Path to stereo_depth_benchmark_*.json")
    parser.add_argument("--output-dir", help="Output directory. Defaults to summary directory.")
    parser.add_argument("--prefix", default="all_models", help="Output filename prefix.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary_path = Path(args.summary_json).expanduser().resolve()
    if not summary_path.exists():
        raise RuntimeError(f"Summary JSON not found: {summary_path}")

    output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else summary_path.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = json.loads(summary_path.read_text())
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Summary JSON has no model rows.")

    prefix = str(args.prefix).strip() or "all_models"
    out_png = output_dir / f"{prefix}_performance_overview.png"
    out_csv = output_dir / f"{prefix}_performance_overview.csv"
    _write_csv(rows, out_csv)
    _plot(rows, out_png)

    print(f"Performance chart: {out_png}")
    print(f"Performance CSV  : {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
