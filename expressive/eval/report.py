"""Metrics and REPORT.md from the cached eval: blind recognition, aliveness, the A/B, the planner's
validity, repairs and latency, and the physical probe suite."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import _core  # noqa: F401
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402
from PIL import Image  # noqa: E402

from brain_client.expressive import presets, probes  # noqa: E402
from brain_client.expressive.motion import Clip  # noqa: E402
from eval.body import Body  # noqa: E402
from eval.prompts import LABELS, PRESET_LABELS, Item, items  # noqa: E402

# The committed report's hand-written analysis starts here; regenerating keeps everything from it on.
HAND_WRITTEN = "## Reading the failures"
COMMITTED = Path(__file__).with_name("REPORT.md")
SUBSETS = (
    ("hand-written preset recipes", lambda i: i.source == "preset"),
    ("planner on the preset prompts", lambda i: i.group == "preset" and i.source == "llm"),
    ("planner on 30 held-out prompts", lambda i: i.group == "ood"),
)
UNFOLDED_CM = 10.0
GESTURING = re.compile(r"\b(reach|point|present|offer|grab|grasp|rais|lift|wav|show)", re.IGNORECASE)
BLUES = LinearSegmentedColormap.from_list("blues", ["#fcfcfb", "#cde2fb", "#86b6ef", "#3987e5", "#1c5cab", "#0d366b"])


@dataclass(frozen=True)
class Scored:
    item: Item
    arm: str
    mean: np.ndarray  # mean probability per label
    descriptions: list[str]
    grades: list[int]
    alive: float
    readable: float

    @property
    def ranked(self) -> list[str]:
        return [LABELS[i] for i in np.argsort(-self.mean)]

    @property
    def top1(self) -> bool:
        return self.ranked[0] in self.item.expects

    @property
    def top3(self) -> bool:
        return bool(set(self.ranked[:3]) & set(self.item.expects))

    @property
    def p_target(self) -> float:
        return float(sum(self.mean[LABELS.index(label)] for label in self.item.expects))

    @property
    def described(self) -> float:
        return float(np.mean([g >= 1 for g in self.grades])) if self.grades else float("nan")

    @property
    def named(self) -> float:
        return float(np.mean([g == 2 for g in self.grades])) if self.grades else float("nan")


def _load(path: Path) -> Any:
    return json.loads(path.read_text()) if path.exists() else None


def score_judge(root: Path, catalog: Sequence[Item], arms: Iterable[str]) -> list[Scored]:
    scored: list[Scored] = []
    for item in catalog:
        for arm in arms:
            readings = _load(root / "readings" / f"{item.id}.{arm}.json")
            if not readings:
                continue
            grades = _load(root / "grades" / f"{item.id}.{arm}.json") or []
            mean = np.mean([[r["probabilities"][label] for label in LABELS] for r in readings], 0)
            scored.append(
                Scored(
                    item=item,
                    arm=arm,
                    mean=mean,
                    descriptions=[r["description"] for r in readings],
                    grades=grades,
                    alive=float(np.mean([r["alive"] for r in readings])),
                    readable=float(np.mean([r["readable"] for r in readings])),
                )
            )
    return scored


def _pct(x: float) -> str:
    return "–" if np.isnan(x) else f"{100 * x:.0f}%"


def _mean(values: Iterable[float]) -> float:
    kept = [v for v in values if not np.isnan(v)]
    return float(np.mean(kept)) if kept else float("nan")


def recognition_table(scored: list[Scored], arms: Sequence[str]) -> tuple[list[str], dict[str, Any]]:
    lines = [
        "| prompts | arm | clips | top-1 | top-3 | p(target) | described ≥ related | named exactly | alive 1-5 | readable 1-5 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    numbers: dict[str, Any] = {}
    for name, keep in SUBSETS:
        for arm in arms:
            rows = [s for s in scored if keep(s.item) and s.arm == arm]
            if not rows:
                continue
            labelled = [s for s in rows if s.item.expects]
            row = {
                "clips": len(rows),
                "labelled": len(labelled),
                "top1": _mean(s.top1 for s in labelled),
                "top3": _mean(s.top3 for s in labelled),
                "p_target": _mean(s.p_target for s in labelled),
                "described": _mean(s.described for s in rows),
                "named": _mean(s.named for s in rows),
                "alive": _mean(s.alive for s in rows),
                "readable": _mean(s.readable for s in rows),
            }
            numbers[f"{name} | {arm}"] = row
            lines.append(
                f"| {name} | {arm} | {row['clips']} | {_pct(row['top1'])} | {_pct(row['top3'])} | "
                f"{_pct(row['p_target'])} | {_pct(row['described'])} | {_pct(row['named'])} | "
                f"{row['alive']:.2f} | {row['readable']:.2f} |"
            )
    return lines, numbers


def ab_table(root: Path, catalog: Sequence[Item], arm: str) -> tuple[list[str], dict[str, Any]]:
    lines = [
        "| prompts | verdicts | lively wins | clear lively wins | clear direct wins | first-shown wins |",
        "|---|---|---|---|---|---|",
    ]
    numbers: dict[str, Any] = {}
    every: list[dict[str, str]] = []
    for name, keep in SUBSETS:
        verdicts = [v for item in catalog if keep(item) for v in (_load(root / "pairs" / f"{item.id}.json") or [])]
        if not verdicts:
            continue
        every += verdicts
        numbers[name] = _ab_row(verdicts, arm)
        lines.append(_ab_line(name, numbers[name]))
    if every:
        numbers["all"] = _ab_row(every, arm)
        lines.append(_ab_line("**all**", numbers["all"]))
    return lines, numbers


def _ab_row(verdicts: list[dict[str, str]], arm: str) -> dict[str, float]:
    wins = [v["winner"] == arm for v in verdicts]
    first = [v["winner"] == v["shown_first"] for v in verdicts]
    return {
        "verdicts": len(verdicts),
        "win": float(np.mean(wins)),
        "clear_win": float(np.mean([w and v["margin"] == "clear" for w, v in zip(wins, verdicts, strict=True)])),
        "clear_loss": float(np.mean([not w and v["margin"] == "clear" for w, v in zip(wins, verdicts, strict=True)])),
        "first_shown_wins": float(np.mean(first)),
    }


def _ab_line(name: str, row: dict[str, float]) -> str:
    return (
        f"| {name} | {row['verdicts']:.0f} | {_pct(row['win'])} | {_pct(row['clear_win'])} | "
        f"{_pct(row['clear_loss'])} | {_pct(row['first_shown_wins'])} |"
    )


def confusion_png(scored: list[Scored], arm: str, png: Path, title: str) -> Path:
    """Mean judge probability per (preset, label): hand-written recipes beside the planner's."""
    names = list(presets.PRESETS)
    panels = [("hand-written recipe", "preset"), ("planner (LLM) recipe", "llm")]
    fig, axes = plt.subplots(1, 2, figsize=(17, 7.6), sharey=True, layout="constrained")
    for ax, (panel, source) in zip(axes, panels, strict=True):
        by_name = {
            s.item.id.split("-", 1)[1]: s
            for s in scored
            if s.arm == arm and s.item.group == "preset" and s.item.source == source
        }
        grid = np.array([by_name[n].mean if n in by_name else np.full(len(LABELS), np.nan) for n in names])
        ax.imshow(grid, cmap=BLUES, vmin=0, vmax=1, aspect="auto")
        for r, name in enumerate(names):
            targets = {LABELS.index(label) for label in PRESET_LABELS[name]}
            for c in range(len(LABELS)):
                value = grid[r, c]
                if np.isnan(value):
                    continue
                if value >= 0.05:
                    ink = "#ffffff" if value > 0.5 else "#0b0b0b"
                    ax.text(
                        c,
                        r,
                        f"{value:.2f}"[1:] if value < 1 else "1",
                        ha="center",
                        va="center",
                        fontsize=7.5,
                        color=ink,
                    )
                if c in targets:
                    ax.add_patch(Rectangle((c - 0.5, r - 0.5), 1, 1, fill=False, edgecolor="#eb6834", linewidth=1.8))
        ax.set_xticks(range(len(LABELS)), LABELS, rotation=55, ha="right", fontsize=9, color="#52514e")
        ax.set_yticks(range(len(names)), names, fontsize=9.5, color="#0b0b0b")
        ax.set_title(panel, fontsize=11, color="#0b0b0b", loc="left")
        ax.tick_params(length=0)
        for side in ax.spines.values():
            side.set_visible(False)
    fig.suptitle(title, fontsize=12, color="#0b0b0b", x=0.01, ha="left")
    png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(png, dpi=110, facecolor="#fcfcfb")
    plt.close(fig)
    return png


def planner_section(out: Path) -> tuple[list[str], dict[str, Any]]:
    plans: dict[str, Any] = _load(out / "plans.json") or {}
    probe_runs: dict[str, list[dict[str, Any]]] = _load(out / "probes.json") or {}
    writes = list(plans.values()) + [w for runs in probe_runs.values() for w in runs]
    if not writes:
        return [], {}
    attempts = np.array([w["attempts"] for w in writes])
    failed = np.array([w["recipe"] is None for w in writes])
    calls = np.array([latency for w in writes for latency in w["latencies"]])
    totals = np.array([sum(w["latencies"]) for w in writes])
    model = writes[0]["model"]
    numbers: dict[str, Any] = {
        "model": model,
        "writes": len(writes),
        "first_pass_valid": float(np.mean(attempts == 1)),
        "repaired_once": float(np.mean((attempts == 2) & ~failed)),
        "repaired_twice": float(np.mean((attempts == 3) & ~failed)),
        "failed": float(np.mean(failed)),
        "call_s": {
            "median": float(np.median(calls)),
            "p90": float(np.percentile(calls, 90)),
            "max": float(calls.max()),
        },
        "write_s": {"median": float(np.median(totals)), "p90": float(np.percentile(totals, 90))},
    }
    lines = [
        f"Planner: `{model}` through `planner.write` (frozen prompt, checker, up to 2 repairs), {len(writes)} writes "
        f"({len(plans)} eval prompts + {sum(len(r) for r in probe_runs.values())} probe samples).",
        "",
        "| first-pass valid | valid after 1 repair | after 2 repairs | no valid recipe | call latency median / p90 / max | write latency median / p90 |",
        "|---|---|---|---|---|---|",
        f"| {_pct(numbers['first_pass_valid'])} | {_pct(numbers['repaired_once'])} | {_pct(numbers['repaired_twice'])} | "
        f"{_pct(numbers['failed'])} | {numbers['call_s']['median']:.1f} / {numbers['call_s']['p90']:.1f} / "
        f"{numbers['call_s']['max']:.1f} s | {numbers['write_s']['median']:.1f} / {numbers['write_s']['p90']:.1f} s |",
    ]
    if probe_runs:
        recipes = {prompt: [w["recipe"] for w in runs] for prompt, runs in probe_runs.items()}
        total, per = probes.score(recipes)
        ood, skill = probes.split(per)
        teacher, _ = probes.score({p: [r] for p, r in probes.TEACHER.items()})
        samples = max(len(r) for r in probe_runs.values())
        numbers["probes"] = {"overall": total, "ood_core": ood, "skill": skill, "teacher": teacher, "per": per}
        lines += [
            "",
            f"Physical probe suite (`brain_client.expressive.probes`), {len(per)} probes × {samples} samples: "
            f"**{_pct(total)}** overall, {_pct(ood)} on the out-of-distribution core, {_pct(skill)} on the rest "
            f"(the hand-written TEACHER recipes score {_pct(teacher)}).",
            "",
            "| probe | pass | checks |",
            "|---|---|---|",
        ]
        lines += [f"| {p.prompt.split('.')[0]} | {_pct(per[p.prompt])} | {p.expects} |" for p in probes.PROBES]
    return lines, numbers


def measure_bodies(out: Path, catalog: Sequence[Item], arm: str = "lively") -> dict[str, dict[str, Any]]:
    """Gripper travel, head and base ranges per clip (cached in body.json, keyed by the clip's content)."""
    path = out / "body.json"
    cache: dict[str, dict[str, Any]] = _load(path) or {}
    body: Body | None = None
    for item in catalog:
        clip_file = out / "clips" / f"{item.id}.{arm}.json"
        if not clip_file.exists():
            continue
        digest = hashlib.sha1(clip_file.read_bytes()).hexdigest()
        if cache.get(item.id, {}).get("hash") == digest:
            continue
        body = body or Body()
        cache[item.id] = {"hash": digest, **asdict(body.measure(Clip.load(clip_file)))}
    path.write_text(json.dumps(cache, indent=1))
    return cache


def body_section(
    out: Path, catalog: Sequence[Item], judged: dict[str, list[Scored]]
) -> tuple[list[str], dict[str, Any]]:
    bodies = measure_bodies(out, catalog)
    lines = [
        "What each hand-written preset does to the body (lively arm, kinematic): how far the gripper travels from "
        "where it starts (base motion removed), the gripper's height range, and the head and base ranges.",
        "",
        "| preset | gripper travel | gripper height | head | base turn | base travel |",
        "|---|---|---|---|---|---|",
    ]
    for item in catalog:
        if item.source != "preset" or item.id not in bodies:
            continue
        b = bodies[item.id]
        lo, hi = b["tip_height_cm"]
        h0, h1 = b["head_deg"]
        lines.append(
            f"| {item.id.split('-', 1)[1]} | {b['tip_cm']:.0f} cm | {lo:.0f}-{hi:.0f} cm | {h0:+.0f}..{h1:+.0f}° | "
            f"{b['base_yaw_deg']:.0f}° | {b['base_x_cm']:.0f} cm |"
        )
    numbers: dict[str, Any] = {}
    lines += [
        "",
        f"Every lively clip split by whether the arm leaves the fold (gripper travel ≥ {UNFOLDED_CM:.0f} cm); "
        "`gesture words` = share of blind descriptions saying reach / point / present / raise / wave / show:",
        "",
        "| judge | arm motion | clips | p(target) | described ≥ related | alive | gesture words |",
        "|---|---|---|---|---|---|---|",
    ]
    for tag, scored in judged.items():
        for label, unfolded in (("stays folded", False), ("unfolds", True)):
            rows = [
                s
                for s in scored
                if s.arm == "lively"
                and s.item.id in bodies
                and (bodies[s.item.id]["tip_cm"] >= UNFOLDED_CM) == unfolded
            ]
            if not rows:
                continue
            words = [bool(GESTURING.search(d)) for s in rows for d in s.descriptions]
            row = {
                "clips": len(rows),
                "p_target": _mean(s.p_target for s in rows if s.item.expects),
                "described": _mean(s.described for s in rows),
                "alive": _mean(s.alive for s in rows),
                "gesture_words": float(np.mean(words)) if words else float("nan"),
            }
            numbers[f"{tag} | {label}"] = row
            lines.append(
                f"| {tag} | {label} | {row['clips']} | {_pct(row['p_target'])} | {_pct(row['described'])} | "
                f"{row['alive']:.2f} | {_pct(row['gesture_words'])} |"
            )
    return lines, numbers


def _strip_block(out: Path, s: Scored) -> list[str]:
    top = ", ".join(f"{label} {s.mean[LABELS.index(label)]:.2f}" for label in s.ranked[:3])
    said = "; ".join(f"“{d}”" for d in s.descriptions)
    return [
        f"**{s.item.id}** ({s.arm}) — prompt: *{s.item.prompt}*  ",
        f"judges said: {said}  ",
        f"labels: {top} · expected {', '.join(s.item.expects) or '(none: description only)'} · "
        f"grades {s.grades} · alive {s.alive:.1f} · readable {s.readable:.1f}",
        "",
        f"![{s.item.id}](strips/{s.item.id}.{s.arm}.png)",
        "",
    ]


def write_report(out: Path, snapshot: Path | None = None) -> Path:
    catalog = items()
    arms = sorted({p.name.split(".")[-2] for p in (out / "clips").glob("*.json")}, key=lambda a: (a != "lively", a))
    lines = [
        "# Blind recognition eval",
        "",
        "Every clip is judged blind: the judge sees the motion (Gemini: the physically simulated video; OpenAI: "
        "a 2×4 key-frame strip) and never the prompt or recipe. It spreads probability over the studio's 17 "
        "labels (`webapp/js/expression/judge.js`), names the motion in its own words, and rates alive / readable "
        "1-5; a text grader then scores each description against the prompt (2 = same feeling or action, "
        "1 = related, 0 = different). `top-1`/`top-3`/`p(target)` count only prompts that have a fitting label; "
        "`described`/`named` count every clip. Arms: **lively** = the plan through procedural liveliness "
        "(what ships), **direct** = the same plan played as-is (control).",
        "",
        "Regenerate: `cd expressive && uv run mars-express eval --judge gemini --n 3` (cached under `out/eval/`).",
        "",
    ]
    core = _load(out / "core.json")
    if core:
        lines += [f"Core library at `{core['core']}` (clips built {core['at']}).", ""]
    metrics: dict[str, Any] = {}
    all_scored: dict[str, list[Scored]] = {}
    judged = sorted((out / "judged").glob("*")) if (out / "judged").exists() else []
    for root in judged:
        scored = score_judge(root, catalog, arms)
        if not scored:
            continue
        tag = root.name
        all_scored[tag] = scored
        rec_lines, rec = recognition_table(scored, arms)
        ab_lines, ab = ab_table(root, catalog, "lively")
        png = confusion_png(
            scored,
            "lively",
            out / f"confusion.{tag}.png",
            f"Blind judge {tag}: mean probability per label (lively arm; orange box = target label)",
        )
        metrics[tag] = {"recognition": rec, "ab": ab}
        lines += [
            f"## Judge `{tag}`",
            "",
            *rec_lines,
            "",
            "A/B (lively vs direct, which moves more like a living creature; order randomised):",
            "",
            *ab_lines,
            "",
            f"![confusion]({png.name})",
            "",
        ]
        lively = [s for s in scored if s.arm == "lively"]
        ranked = sorted(lively, key=lambda s: (_mean(s.grades) if s.grades else 0.0) + s.p_target)
        lines += ["### Clearest reads", ""]
        for s in ranked[::-1][:3]:
            lines += _strip_block(out, s)
        lines += ["### Worst reads", ""]
        for s in ranked[:4]:
            lines += _strip_block(out, s)
    if all_scored:
        body_lines, metrics["body"] = body_section(out, catalog, all_scored)
        lines += ["## What the body does", "", *body_lines, ""]
    planner_lines, planner_numbers = planner_section(out)
    metrics["planner"] = planner_numbers
    lines += ["## Planner", "", *planner_lines, ""]
    if COMMITTED.exists() and HAND_WRITTEN in (committed := COMMITTED.read_text()):
        lines += ["", committed[committed.index(HAND_WRITTEN) :].rstrip()]
    (out / "metrics.json").write_text(json.dumps(metrics, indent=1, default=float))
    report = out / "REPORT.md"
    report.write_text("\n".join(lines) + "\n")
    if snapshot is not None:
        _snapshot(out, report, snapshot)
    return report


def _snapshot(out: Path, report: Path, dest: Path) -> None:
    """A committable copy of the report: every referenced image as a compressed JPEG beside it."""
    text = report.read_text()
    figures = dest / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    for name in sorted({part.split(")")[0] for part in text.split("](")[1:] if part.split(")")[0].endswith(".png")}):
        source = out / name
        if not source.exists():
            continue
        target = figures / (Path(name).stem + ".jpg")
        image = Image.open(source).convert("RGB")
        image.thumbnail((1100, 1100))
        image.save(target, quality=70, optimize=True)
        text = text.replace(f"]({name})", f"](figures/{target.name})")
    (dest / "REPORT.md").write_text(text)
    shutil.copy(out / "metrics.json", dest / "metrics.json")
