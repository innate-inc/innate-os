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
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np

from .common import DESCRIPTORS, agreement, descriptors, parse
from .sft import DATA


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

    from ..generator.evaluate import HeldOut

    captions = HeldOut().prompts
    real = dict(
        zip(
            captions,
            (parse(o)[1] for o in planner.complete(list(captions.values()), 0.0, [0] * len(captions))),
            strict=True,
        )
    )

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
    shown = {k: v for k, v in report.items() if k not in ("probe_recipes", "val_recipes", "agree_per_descriptor")}
    print(json.dumps(shown, indent=1))
    if a.out:
        a.out.write_text(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
