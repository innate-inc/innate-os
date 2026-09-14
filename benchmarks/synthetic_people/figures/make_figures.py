#!/usr/bin/env python3
"""Render publication-style figures directly from frozen benchmark outputs."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
MODELS = [
    ("astra-low", "Astra 6", "#6257CB"),
    ("gemini-low", "Gemini 3.6 Flash", "#008A82"),
    ("pr807-face", "PR #807 face path", "#C78A28"),
]
TEXT = "#172536"
MUTED = "#526173"
GRID = "#E4E9EF"
plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",
        "font.size": 14,
        "text.color": TEXT,
        "axes.labelcolor": MUTED,
        "xtick.color": MUTED,
        "ytick.color": TEXT,
        "axes.edgecolor": GRID,
        "figure.facecolor": "#FFFFFF",
        "axes.facecolor": "#FFFFFF",
        "svg.fonttype": "none",
        "savefig.facecolor": "#FFFFFF",
    }
)
DATA = {}
INPUTS = {}
for run, _, _ in MODELS + [("pr807-hog-face", "", "")]:
    p = ROOT / "results" / run / "predictions.jsonl"
    DATA[run] = [json.loads(line) for line in p.read_text().splitlines()]
    INPUTS[str(p.relative_to(ROOT))] = hashlib.sha256(p.read_bytes()).hexdigest()


def count_known(run, condition):
    rows = [r for r in DATA[run] if r["condition"] == condition and r["expected"] != "UNKNOWN"]
    assert len(rows) == 15
    return sum(r["label"] == r["expected"] for r in rows)


def draw_recognition(ax, conditions, labels, panel_title):
    y = np.arange(len(conditions))
    offsets = [-0.235, 0, 0.235]
    for index, (run, _, color) in enumerate(MODELS):
        values = [count_known(run, c) for c in conditions]
        positions = y + offsets[index]
        ax.barh(positions, values, height=0.185, color=color, zorder=3)
        for pos, value in zip(positions, values, strict=True):
            if value == 0:
                ax.scatter([0], [pos], s=18, color=color, clip_on=False, zorder=4)
            ax.text(value + 0.2, pos, f"{value}/15", ha="left", va="center", fontsize=13.5, color=TEXT)
    ax.set_yticks(y, labels)
    ax.set_ylim(len(y) - 0.48, -0.57)
    ax.set_xlim(0, 17.5)
    ax.set_xticks([0, 5, 10, 15])
    ax.set_xlabel("Correctly recognized enrolled characters", labelpad=12, fontsize=13)
    ax.tick_params(axis="both", length=0, pad=9)
    ax.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.set_title(panel_title, loc="left", fontweight="bold", fontsize=16, pad=19)


def save(fig, name):
    fig.savefig(HERE / f"{name}.png", dpi=190)
    fig.savefig(HERE / f"{name}.svg")
    plt.close(fig)


fig = plt.figure(figsize=(11, 12.3))
fig.text(0.075, 0.966, "Recognition by image detail", fontsize=27, fontweight="bold", va="top")
fig.text(0.075, 0.917, "15 enrolled fictional characters per condition  ·  Higher is better", fontsize=14, color=MUTED)
fig.legend(
    handles=[Patch(facecolor=c, label=n) for _, n, c in MODELS],
    loc="upper left",
    bbox_to_anchor=(0.067, 0.906),
    ncol=3,
    frameon=False,
    fontsize=13,
    handlelength=1.2,
    columnspacing=1.8,
)
ax_face = fig.add_axes((0.225, 0.565, 0.71, 0.265))
draw_recognition(
    ax_face, ["face_64", "face_32", "face_16"], ["64 × 64 px", "32 × 32 px", "16 × 16 px"], "Face-only crops"
)
ax_scene = fig.add_axes((0.225, 0.13, 0.71, 0.335))
draw_recognition(
    ax_scene,
    ["near_320", "near_160", "far_320", "far_160"],
    ["Near\n320 × 240", "Near\n160 × 120", "Distant\n320 × 240", "Distant\n160 × 120"],
    "Robot-style full scenes",
)
fig.text(0.075, 0.04, "PR face path = YuNet + SFace, without HOG person detection.", color=MUTED, fontsize=12)
fig.text(
    0.075,
    0.017,
    "Synthetic views only. Face-crop dimensions and scene dimensions are different quantities.",
    color=MUTED,
    fontsize=12,
)
save(fig, "recognition-by-detail")

# Unknown-only outcomes; absence of a detected face is an abstention, not a correct rejection.
CATEGORIES = [
    ("false_name", "Incorrect known name", "#E9796D"),
    ("abstain", "Abstained", "#D7DDE5"),
    ("correct_unknown", "Correct “unknown”", "#65B4A0"),
]
fig = plt.figure(figsize=(11, 5.4))
fig.text(0.075, 0.94, "Unknown characters: what did each method do?", fontsize=23, fontweight="bold", va="top")
fig.text(
    0.075, 0.86, "35 held-out queries per method  ·  5 fictional characters × 7 conditions", fontsize=13, color=MUTED
)
fig.legend(
    handles=[Patch(facecolor=c, label=n) for _, n, c in CATEGORIES],
    loc="upper left",
    bbox_to_anchor=(0.067, 0.83),
    ncol=3,
    frameon=False,
    fontsize=12.5,
    handlelength=1.2,
    columnspacing=1.4,
)
ax = fig.add_axes((0.25, 0.245, 0.67, 0.45))
unknown_counts = {}
for row, (run, _, _) in enumerate(MODELS):
    unknown = [r for r in DATA[run] if r["expected"] == "UNKNOWN"]
    assert len(unknown) == 35
    counts = Counter(
        "correct_unknown" if r["label"] == "UNKNOWN" else "abstain" if r["label"] == "UNCERTAIN" else "false_name"
        for r in unknown
    )
    unknown_counts[run] = dict(counts)
    left = 0
    for key, _, color in CATEGORIES:
        val = counts[key]
        if val:
            ax.barh(row, val, left=left, height=0.56, color=color)
            ax.text(left + val / 2, row, str(val), ha="center", va="center", color=TEXT, fontsize=16, fontweight="bold")
        left += val
    assert left == 35
ax.set_yticks(range(3), [m[1] for m in MODELS])
ax.set_ylim(2.65, -0.65)
ax.set_xlim(0, 35)
ax.set_xticks([0, 5, 10, 15, 20, 25, 30, 35])
ax.set_xlabel("Number of held-out queries", labelpad=11, fontsize=12)
ax.tick_params(length=0, pad=9)
for s in ax.spines.values():
    s.set_visible(False)
fig.text(
    0.075,
    0.083,
    "Neither LLM correctly returned “unknown”; all remaining cases were abstentions.",
    fontsize=12,
    color=TEXT,
)
fig.text(
    0.075,
    0.029,
    "Lookalike / generation ambiguity affects these counts. They are not real-world error rates.",
    fontsize=12,
    color=MUTED,
)
save(fig, "unknown-outcomes")

reasons = Counter(r["reason"] for r in DATA["pr807-hog-face"])
assert reasons == {"no_person": 47, "below_24px": 28, "no_usable_face": 4, "score_or_margin_reject": 1}
pipeline = """flowchart TD
    A["80 synthetic scene queries"] --> B["33: person detected"]
    A --> X["47: no person detected"]
    B --> C["1: usable face embedding"]
    B --> Y["28: face below 24 px"]
    B --> Z["4: no usable face"]
    C --> D["1: known character rejected"]
    D --> E["0 accepted identities"]
"""
(HERE / "pr807-pipeline.mmd").write_text(pipeline)
(HERE / "figure_data.json").write_text(
    json.dumps(
        {
            "source_sha256": INPUTS,
            "known_correct": {
                run: {
                    c: count_known(run, c)
                    for c in ["face_64", "face_32", "face_16", "near_320", "near_160", "far_320", "far_160"]
                }
                for run, _, _ in MODELS
            },
            "unknown_outcomes": unknown_counts,
            "hog_face_pipeline_reasons": dict(reasons),
            "matplotlib_version": matplotlib.__version__,
        },
        indent=2,
    )
    + "\n"
)
print("Created two figures in PNG/SVG and the PR pipeline diagram from saved predictions.")
