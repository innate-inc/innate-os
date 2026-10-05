"""Score a planner on the held-out spoken replies (speech_val.jsonl): run in envs/serve (vLLM).

  python -m ml.distill.speech_eval runs/planner-4b-speech/served --fp8 --spec-tokens 3 --out runs/.../speech_eval.json
  python -m ml.distill.speech_eval --teacher --out runs/teacher_speech_eval.json       # the teacher's own answers

valid     answers that parse and pass the recipe checker (greedy, no retries)
cues      a body check per physical cue the reply's writer labelled (yes nods, no shakes the base, hello/bye wave,
          look orients the base or drops the gaze, come advances or leans in, think cants with the gaze up, laugh bobs);
          one swing counts: a spoken "No." gets a one-cycle shake, where the emote checks in evaluate.py want two
size      how big each performance is (``reach``: the arm posture's largest excursion, ``e_max``: the energy peak) per
          writer label (beat / continue / peak); ``big`` = reach >= BIG_REACH or e_max >= BIG_ENERGY
frantic   the share of beat / continue lines performed big, and of replies with two big lines in a row
agree     per-descriptor Pearson r with the teacher's recipe on the same row
tokens    output tokens per answer (the latency driver) and the idea's word count
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from brain_client.expressive.channels import FPS, Ch, Frames
from brain_client.expressive.dsl import expand

from .common import agreement, descriptors, parse
from .evaluate import swings
from .sft import DATA

BIG_REACH = 0.5
BIG_ENERGY = 4.0

CUE_CHECKS: dict[str, Callable[[Frames], bool]] = {
    "yes": lambda f: swings(f[:, Ch.ATTEND], 0.12) >= 1,
    "no": lambda f: swings(f[:, Ch.ORIENT], 6.0) >= 1,
    "hello": lambda f: f[:, Ch.RISE].max() >= 0.3 and swings(f[:, Ch.ASKEW], 0.2) >= 1,
    "bye": lambda f: f[:, Ch.RISE].max() >= 0.3 and swings(f[:, Ch.ASKEW], 0.2) >= 1,
    "look": lambda f: np.abs(f[:, Ch.ORIENT]).max() >= 12 or f[:, Ch.ATTEND].min() <= -0.25,
    "come": lambda f: f[:, Ch.ADVANCE].max() >= 0.04 or f[:, Ch.APPROACH].max() >= 0.35,
    "think": lambda f: np.abs(f[:, Ch.ASKEW]).max() >= 0.25 and f[:, Ch.ATTEND].max() >= 0.2,
    "laugh": lambda f: swings(f[:, Ch.RISE], 0.1) >= 1,
}


def reach(f: Frames) -> float:
    return float(np.abs(f[:, [Ch.APPROACH, Ch.EXPAND, Ch.RISE, Ch.ASKEW]]).max())


def big(f: Frames) -> bool:
    return reach(f) >= BIG_REACH or f[:, Ch.ENERGY].max() >= BIG_ENERGY


def speech_report(rows: list[dict[str, Any]], recipes: list[str | None], ideas: list[str]) -> dict[str, Any]:
    """Held-out rows (with the writer's labels and the teacher's recipe) against one planner's answers."""
    frames = [expand(r) if r else None for r in recipes]
    ok = [(row, f) for row, f in zip(rows, frames, strict=True) if f is not None]
    cues = {
        cue: [f is not None and bool(check(f)) for row, f in zip(rows, frames, strict=True) if row["cue"] == cue]
        for cue, check in CUE_CHECKS.items()
    }
    sizes: dict[str, dict[str, float]] = {}
    for label in ("beat", "continue", "peak"):
        mine = [f for row, f in ok if row["size"] == label]
        if mine:
            sizes[label] = {
                "rows": len(mine),
                "reach": float(np.mean([reach(f) for f in mine])),
                "e_max": float(np.mean([f[:, Ch.ENERGY].max() for f in mine])),
                "seconds": float(np.mean([(len(f) - 1) / FPS for f in mine])),
                "big": float(np.mean([big(f) for f in mine])),
            }
    by_reply: dict[str, list[bool]] = {}
    for row, f in zip(rows, frames, strict=True):
        by_reply.setdefault(row["reply"], []).append(f is not None and big(f))
    long_replies = [b for b in by_reply.values() if len(b) >= 2]
    small_rows = [big(f) for row, f in ok if row["size"] in ("beat", "continue")]
    pairs = [(descriptors(rec), descriptors(row["recipe"])) for row, rec in zip(rows, recipes, strict=True) if rec]
    words = [len(r["sentence"].split()) for r, _ in ok]
    return {
        "rows": len(rows),
        "valid": len(ok) / max(1, len(rows)),
        "cues": {cue: float(np.mean(v)) for cue, v in cues.items() if v},
        "cue_rows": {cue: len(v) for cue, v in cues.items() if v},
        "cues_mean": float(np.mean([np.mean(v) for v in cues.values() if v])),
        "sizes": sizes,
        "frantic_small_lines_big": float(np.mean(small_rows)) if small_rows else float("nan"),
        "frantic_replies_two_big_in_a_row": float(
            np.mean([any(a and b for a, b in zip(r, r[1:], strict=False)) for r in long_replies])
        ),
        "big_per_reply": float(np.mean([sum(r) for r in by_reply.values()])),
        "seconds_per_word": float(
            np.median([(len(f) - 1) / FPS / max(1, w) for (_, f), w in zip(ok, words, strict=True)])
        ),
        "agree": float(np.nanmean(agreement([a for a, _ in pairs], [b for _, b in pairs]))),
        "idea_words": float(np.median([len(i.split()) for i in ideas])) if ideas else float("nan"),
    }


def run_planner(
    path: Path, rows: list[dict[str, Any]], fp8: bool, spec_tokens: int, gpu_share: float
) -> dict[str, Any]:
    from ..engine import Planner

    planner = Planner(str(path), gpu_memory_utilization=gpu_share, fp8=fp8, spec_tokens=spec_tokens)
    texts = planner.complete([r["prompt"] for r in rows], 0.0, [0] * len(rows))
    parsed = [parse(t) for t in texts]
    latency = []
    for row in rows[:64]:
        start = time.perf_counter()
        planner.complete([row["prompt"]], 0.0, [0])
        latency.append(time.perf_counter() - start)
    tokens = [len(planner.tok(t, add_special_tokens=False).input_ids) for t in texts]
    return {
        "texts": texts,
        "ideas": [i for i, _ in parsed],
        "recipes": [r for _, r in parsed],
        "tokens_median": float(statistics.median(tokens)),
        "tokens_p95": float(np.percentile(tokens, 95)),
        "offline_latency_ms_median": 1e3 * statistics.median(latency),
        "offline_latency_ms_p95": 1e3 * float(np.percentile(latency, 95)),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ml.distill.speech_eval", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("planner", type=Path, nargs="?")
    ap.add_argument("--teacher", action="store_true", help="score the teacher's answers in speech_val.jsonl")
    ap.add_argument("--val", type=Path, default=DATA / "speech_val.jsonl")
    ap.add_argument("--fp8", action="store_true")
    ap.add_argument("--spec-tokens", type=int, default=0)
    ap.add_argument("--gpu-share", type=float, default=0.3)
    ap.add_argument("--out", type=Path)
    a = ap.parse_args()
    rows = [json.loads(line) for line in a.val.read_text().splitlines() if line.strip()]
    if a.teacher:
        answers: dict[str, Any] = {"ideas": [r["idea"] for r in rows], "recipes": [r["recipe"] for r in rows]}
    elif a.planner:
        answers = run_planner(a.planner, rows, a.fp8, a.spec_tokens, a.gpu_share)
    else:
        raise SystemExit("give a planner directory or --teacher")
    report = {"planner": str(a.planner or "teacher"), **speech_report(rows, answers["recipes"], answers["ideas"])}
    report |= {k: v for k, v in answers.items() if k not in ("texts", "ideas", "recipes")}
    print(json.dumps(report, indent=1))
    if a.out:
        per_row = {
            r["id"]: {"idea": i, "recipe": rec}
            for r, i, rec in zip(rows, answers["ideas"], answers["recipes"], strict=True)
        }
        a.out.write_text(json.dumps({**report, "answers": per_row, "texts": answers.get("texts")}, indent=1))


if __name__ == "__main__":
    main()
