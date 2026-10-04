"""Score a planner on prompts it never trained on (run in envs/serve: vLLM).

python -m ml.distill.evaluate runs/planner-4b/merged --out runs/planner-4b/eval.json
python -m ml.distill.evaluate --teacher W/teacher_probes.json     # the teacher's probe recipes, same scoring

probes   the core probe suite (physical checks on out-of-distribution prompts), --samples per prompt at T = 0.7
agree    mean per-descriptor Pearson r between the planner's greedy recipes and the teacher's on the val prompts,
         next to the teacher's agreement with its own second recipe (the ceiling)
valid    share of answers that parse and pass the recipe checker (no retries)
latency  median single-prompt greedy decode time (offline engine, warm prefix cache)
real     greedy recipes for the 12 held-out real emotions -> 5 training-style plans each -> generator -> rank of the
         true clip among the 12 (Binh's teacher-independent check; chance 8.3% top-1, mean rank 6.5)
talk     the held-out conversational situations (conv_val.jsonl): validity, agreement with the teacher, and a body
         check per beat (refuse/disagree shake the base, agree nods, waves sway the raised arm, ...) for both
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np

from brain_client.expressive.channels import Ch, Frames
from brain_client.expressive.dsl import expand

from .common import DESCRIPTORS, agreement, descriptors, parse
from .sft import DATA


def swings(x: Frames, amp: float) -> int:
    """Crossings of the detrended signal (0.5 s running mean) from above ``amp / 2`` to below ``-amp / 2`` or back."""
    window = 13
    if len(x) <= window:
        return 0
    trend = np.convolve(np.pad(x, window // 2, mode="edge"), np.ones(window) / window, "valid")
    side, count = 0, 0
    for value in x - trend:
        if abs(value) > amp / 2 and np.sign(value) != side:
            count += side != 0
            side = int(np.sign(value))
    return count


BEAT_CHECKS: dict[str, tuple[str, Any]] = {
    "talk/refuse": ("base shakes (>= 2 orient swings of 8 deg)", lambda f: swings(f[:, Ch.ORIENT], 8.0) >= 2),
    "talk/disagree": ("base shakes (>= 2 orient swings of 8 deg)", lambda f: swings(f[:, Ch.ORIENT], 8.0) >= 2),
    "talk/agree": ("nods (>= 2 attend swings of .15)", lambda f: swings(f[:, Ch.ATTEND], 0.15) >= 2),
    "talk/hello": (
        "waves (raised arm, >= 2 askew swings)",
        lambda f: f[:, Ch.RISE].max() >= 0.3 and swings(f[:, Ch.ASKEW], 0.2) >= 2,
    ),
    "talk/goodbye": (
        "waves (raised arm, >= 2 askew swings)",
        lambda f: f[:, Ch.RISE].max() >= 0.3 and swings(f[:, Ch.ASKEW], 0.2) >= 2,
    ),
    "talk/listen": ("gaze on the face (mean attend >= .15)", lambda f: f[:, Ch.ATTEND].mean() >= 0.15),
    "talk/pardon": ("leans in (approach >= .3)", lambda f: f[:, Ch.APPROACH].max() >= 0.3),
    "talk/success": ("rises tall (rise >= .4)", lambda f: f[:, Ch.RISE].max() >= 0.4),
    "talk/failure": (
        "sinks (rise <= -.2 or attend <= -.3)",
        lambda f: f[:, Ch.RISE].min() <= -0.2 or f[:, Ch.ATTEND].min() <= -0.3,
    ),
}


def beat_pass(beat: str, recipe: str | None) -> bool:
    """The body check of a beat in ``BEAT_CHECKS`` on one recipe (a missing recipe fails)."""
    return bool(recipe) and bool(BEAT_CHECKS[beat][1](expand(recipe)))


def talk_report(rows: list[dict[str, Any]], answers: list[str | None]) -> dict[str, Any]:
    """Held-out conversational situations: the student's greedy recipes against the teacher's."""
    ok = [(r, a) for r, a in zip(rows, answers, strict=True) if a]
    per_beat: dict[str, dict[str, float]] = {}
    for beat in sorted({r["beat"] for r in rows} & set(BEAT_CHECKS)):
        mine = [(r, a) for r, a in zip(rows, answers, strict=True) if r["beat"] == beat]
        per_beat[beat] = {
            "student": float(np.mean([beat_pass(beat, a) for _, a in mine])),
            "teacher": float(np.mean([beat_pass(beat, r["recipe"]) for r, _ in mine])),
        }
    student = [v["student"] for v in per_beat.values()]
    teacher = [v["teacher"] for v in per_beat.values()]
    agree = agreement([descriptors(a) for _, a in ok], [descriptors(r["recipe"]) for r, _ in ok])
    return {
        "prompts": len(rows),
        "valid": len(ok) / max(1, len(rows)),
        "agree": float(np.nanmean(agree)),
        "beat_checks_student": float(np.mean(student)) if student else float("nan"),
        "beat_checks_teacher": float(np.mean(teacher)) if teacher else float("nan"),
        "per_beat": per_beat,
    }


def probe_report(recipes: dict[str, list[str | None]]) -> dict[str, Any]:
    from brain_client.expressive.probes import score, split

    mean, per = score(recipes)
    ood, skill = split(per)
    return {"mean": mean, "ood_core": ood, "skill": skill, "per_probe": per}


def real_clip_report(recipes: dict[str, str | None], generator: Path) -> dict[str, float]:
    """``{held-out clip name: recipe}`` -> identification of each clip from its planner-written recipe."""
    from brain_client.expressive.dsl import variants
    from brain_client.expressive.plan import EXTRACT_FC, EXTRACT_KDT

    from ..generator.evaluate import HeldOut
    from ..generator.sample import Generator

    held, gen, ranks, per_recipe = HeldOut(), Generator(generator), [], 5
    for name, recipe in recipes.items():
        if not recipe:
            ranks += [len(held.real)] * per_recipe
            continue
        plans = variants(recipe, per_recipe, seed=1, kdt=EXTRACT_KDT, fc=EXTRACT_FC)
        ranks += [held.rank(m, name) for m in gen.generate_batch(plans, seeds=list(range(per_recipe)))]
    r = np.array(ranks)
    return {"top1": float((r == 1).mean()), "mean_rank": float(r.mean())}


def evaluate(
    planner_path: Path, val_file: Path, samples: int, generator: Path, gpu_share: float, fp8: bool = False
) -> dict[str, Any]:
    from brain_client.expressive.probes import PROBES

    from ..engine import Planner
    from ..generator.evaluate import HeldOut

    planner = Planner(str(planner_path), gpu_memory_utilization=gpu_share, fp8=fp8)
    val = [json.loads(line) for line in val_file.read_text().splitlines() if line.strip()]
    greedy = planner.complete([v["prompt"] for v in val], temperature=0.0, seeds=list(range(len(val))))
    ok = [(v, rec) for v, (_, rec) in zip(val, map(parse, greedy), strict=True) if rec]
    student = agreement([descriptors(r) for _, r in ok], [descriptors(v["labels"][0]["recipe"]) for v, _ in ok])
    teacher = agreement(
        [descriptors(v["labels"][0]["recipe"]) for v in val], [descriptors(v["labels"][1]["recipe"]) for v in val]
    )

    per_probe: dict[str, list[str | None]] = {}
    for probe in PROBES:
        outs = planner.complete([probe.prompt] * samples, temperature=0.7, seeds=list(range(1, samples + 1)))
        per_probe[probe.prompt] = [parse(o)[1] for o in outs]

    latency = []
    for v in val[:16]:
        start = time.perf_counter()
        planner.complete([v["prompt"]], temperature=0.0, seeds=[0])
        latency.append(time.perf_counter() - start)

    captions = HeldOut().prompts
    real = dict(
        zip(
            captions,
            (parse(o)[1] for o in planner.complete(list(captions.values()), 0.0, [0] * len(captions))),
            strict=True,
        )
    )

    talk_rows = [json.loads(line) for line in (DATA / "conv_val.jsonl").read_text().splitlines() if line.strip()]
    talk_answers = [parse(o)[1] for o in planner.complete([r["prompt"] for r in talk_rows], 0.0, [0] * len(talk_rows))]

    answers = len(val) + sum(map(len, per_probe.values()))
    valid = (len(ok) + sum(r is not None for rs in per_probe.values() for r in rs)) / answers
    return {
        "planner": str(planner_path),
        "valid": valid,
        "agree": float(np.nanmean(student)),
        "agree_teacher_ceiling": float(np.nanmean(teacher)),
        "agree_per_descriptor": dict(zip(DESCRIPTORS, student, strict=True)),
        "probes": probe_report(per_probe),
        "latency_ms_median": 1e3 * statistics.median(latency),
        "real_clips": real_clip_report(real, generator),
        "talk": talk_report(talk_rows, talk_answers),
        "talk_recipes": dict(zip([r["prompt"] for r in talk_rows], talk_answers, strict=True)),
        "probe_recipes": per_probe,
        "val_recipes": dict(zip([v["prompt"] for v in val], greedy, strict=True)),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ml.distill.evaluate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("planner", type=Path, nargs="?")
    ap.add_argument("--teacher", type=Path, help="score the teacher's probe recipes ({prompt: [recipe, ...]}) instead")
    ap.add_argument(
        "--teacher-real", type=Path, help="with --teacher: its recipes for the 12 held-out clips {name: recipe}"
    )
    ap.add_argument("--generator", type=Path, default=Path("runs/generator/generator.pt"))
    ap.add_argument("--gpu-share", type=float, default=0.5, help="GPU memory fraction for the planner's vLLM engine")
    ap.add_argument("--fp8", action="store_true", help="score the FP8-quantised planner (as served with --fp8)")
    ap.add_argument("--val", type=Path, default=DATA / "val.jsonl")
    ap.add_argument("--samples", type=int, default=12)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    if a.teacher:
        report: dict[str, Any] = {"planner": "teacher", "probes": probe_report(json.loads(a.teacher.read_text()))}
        if a.teacher_real:
            report["real_clips"] = real_clip_report(json.loads(a.teacher_real.read_text()), a.generator)
    elif a.planner:
        report = evaluate(a.planner, a.val, a.samples, a.generator, a.gpu_share, a.fp8)
    else:
        raise SystemExit("give a planner directory or --teacher")
    hidden = ("probe_recipes", "val_recipes", "talk_recipes", "agree_per_descriptor")
    shown = {k: v for k, v in report.items() if k not in hidden}
    print(json.dumps(shown, indent=1))
    if a.out:
        a.out.write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
