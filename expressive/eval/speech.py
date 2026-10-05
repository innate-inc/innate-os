"""Blind judging of speech planners on the held-out spoken replies (``ml/distill_data/speech_val.jsonl``).

  uv run --extra flow python -m eval.speech --arm current=talk4b.json --arm s4=s4.json --arm s08=s08.json \\
      --pair current,s4 --pair s4,teacher --judge openai --out out/speech_eval

Arms are ``name=answers.json`` from ``ml.distill.speech_eval --out`` (``teacher`` is built in: the recipes in the val
file). Every recipe plays through the flow generator, as the server sends it, and is filmed as a key-frame strip
(openai) or a video (gemini). Per row the judge sees the line with its context:

fit       ``n`` ratings 1-5 of how well the motion fits the line
ab        per pair of arms, both orders; a win counts only when both orders agree
identify  which of four lines the robot was saying (the row's + three other held-out lines, the same options for
          every arm; chance 25 %)

Everything is cached under ``--out``; ``report.md`` and ``metrics.json`` are rewritten at the end.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import _core  # noqa: F401
import numpy as np

from eval.arms import flow, make_clip
from eval.judge import Judge, JudgeKind, Media, Verdict
from eval.llm import load_env
from eval.run import _parallel, _read, _render_video, _write
from eval.strips import DEFAULT_CAMERA, Filmstrip

logger = logging.getLogger("mars-express.speech")
VAL = Path(__file__).parents[1] / "ml" / "distill_data" / "speech_val.jsonl"
Row = dict[str, Any]


def slug(row_id: str) -> str:
    return row_id.replace("#", "_")


def line_of(row: Row) -> str:
    text = f'Line: "{row["sentence"]}"'
    if row.get("before"):
        text += f'\n(Its previous sentence: "{row["before"]}")'
    if row.get("heard"):
        text += f'\n(The person had just said: "{row["heard"]}")'
    return text


def options_of(row: Row, rows: list[Row]) -> tuple[list[str], int]:
    """Four lines (the row's + three others), shuffled by the row's id: the same for every arm."""
    rng = random.Random(zlib.crc32(row["id"].encode()))
    others = rng.sample([r["sentence"] for r in rows if r["sentence"] != row["sentence"]], 3)
    lines = [*others, row["sentence"]]
    rng.shuffle(lines)
    return lines, lines.index(row["sentence"])


def load_arms(specs: list[str], rows: list[Row]) -> dict[str, dict[str, str | None]]:
    arms: dict[str, dict[str, str | None]] = {"teacher": {r["id"]: r["recipe"] for r in rows}}
    for spec in specs:
        name, _, path = spec.partition("=")
        answers = json.loads(Path(path).read_text())["answers"]
        arms[name] = {r["id"]: answers.get(r["id"], {}).get("recipe") for r in rows}
    return arms


def build_media(
    out: Path, rows: list[Row], arms: dict[str, dict[str, str | None]], videos: bool, workers: int
) -> dict[tuple[str, str], Media]:
    generate = flow(Path(__file__).parents[1] / "out" / "models" / "generator.pt")
    strip = Filmstrip()
    media: dict[tuple[str, str], Media] = {}
    jobs: list[tuple[str, str, str, str]] = []
    for row in rows:
        for arm, recipes in arms.items():
            recipe = recipes[row["id"]]
            if not recipe:
                continue
            key = f"{slug(row['id'])}.{arm}"
            clip_path = out / "clips" / f"{key}.json"
            m = Media(out / "strips" / f"{key}.png", out / "videos" / f"{key}.mp4")
            if not clip_path.exists():
                seed = zlib.crc32(row["id"].encode()) & 0xFFFF
                make_clip(recipe, generate, seed, name=key, prompt=row["prompt"]).save(clip_path)
            if not m.strip.exists():
                from brain_client.expressive.motion import Clip

                strip.render(Clip.load(clip_path), m.strip)
            if videos and not m.video.exists():
                jobs.append((str(clip_path), str(m.video), DEFAULT_CAMERA, ""))
            media[(row["id"], arm)] = m
    logger.info("media: %d clips, rendering %d videos", len(media), len(jobs))
    with ProcessPoolExecutor(workers) as pool:
        list(pool.map(_render_video, jobs))
    return media


def judge_all(
    out: Path, judge: Judge, rows: list[Row], media: dict[tuple[str, str], Media], pairs: list[tuple[str, str]], n: int
) -> Path:
    root = out / "judged" / f"{judge.kind}-{judge.model}"
    by_id = {r["id"]: r for r in rows}

    def fit(job: tuple[str, str]) -> None:
        path = root / "fit" / f"{slug(job[0])}.{job[1]}.json"
        done: list[list[Any]] = _read(path) or []
        while len(done) < n:
            done.append(list(judge.fit(media[job], line_of(by_id[job[0]]))))
            _write(path, done)

    def identify(job: tuple[str, str]) -> None:
        path = root / "identify" / f"{slug(job[0])}.{job[1]}.json"
        if path.exists():
            return
        lines, truth = options_of(by_id[job[0]], rows)
        _write(path, {"correct": judge.identify(media[job], lines) == truth})

    def ab(job: tuple[str, tuple[str, str]]) -> None:
        row_id, (a, b) = job
        path = root / "ab" / f"{slug(row_id)}.{a}-{b}.json"
        rounds: list[list[Verdict]] = _read(path) or []
        shown = ((a, media[(row_id, a)]), (b, media[(row_id, b)]))
        line = line_of(by_id[row_id])
        while len(rounds) < max(1, n // 2):
            rounds.append([judge.fit_pair(*shown, line), judge.fit_pair(*shown[::-1], line)])
            _write(path, rounds)

    keys = sorted(media)
    workers = 16
    _parallel(keys, fit, workers, "fit")
    _parallel(keys, identify, workers, "identify")
    matches = [(r["id"], p) for r in rows for p in pairs if (r["id"], p[0]) in media and (r["id"], p[1]) in media]
    _parallel(matches, ab, workers, "ab")
    return root


def report(root: Path, rows: list[Row], arms: list[str], pairs: list[tuple[str, str]]) -> dict[str, Any]:
    out: dict[str, Any] = {"arms": {}, "pairs": {}}
    for arm in arms:
        fits = {r["id"]: _read(root / "fit" / f"{slug(r['id'])}.{arm}.json") for r in rows}
        ident = [_read(root / "identify" / f"{slug(r['id'])}.{arm}.json") for r in rows]
        per_size = {}
        for size in ("beat", "continue", "peak"):
            scores = [np.mean([f for f, _ in fits[r["id"]]]) for r in rows if r["size"] == size and fits[r["id"]]]
            per_size[size] = float(np.mean(scores)) if scores else float("nan")
        scored = [np.mean([f for f, _ in v]) for v in fits.values() if v]
        hits = [i["correct"] for i in ident if i]
        out["arms"][arm] = {
            "fit": float(np.mean(scored)) if scored else float("nan"),
            "fit_by_size": per_size,
            "identify": float(np.mean(hits)) if hits else float("nan"),
            "rows": len(scored),
        }
    for a, b in pairs:
        wins = {a: 0, b: 0, "split": 0}
        for r in rows:
            for first, second in _read(root / "ab" / f"{slug(r['id'])}.{a}-{b}.json") or []:
                if first["winner"] == second["winner"]:
                    wins[first["winner"]] += 1
                else:
                    wins["split"] += 1
        agreed = wins[a] + wins[b]
        out["pairs"][f"{a}-{b}"] = {**wins, f"{a}_share_of_agreed": wins[a] / agreed if agreed else float("nan")}
    return out


def markdown(metrics: dict[str, Any]) -> str:
    lines = [
        "| arm | fit 1-5 | beat | continue | peak | identify (chance 25%) | rows |",
        "|---|---|---|---|---|---|---|",
    ]
    for arm, m in metrics["arms"].items():
        s = m["fit_by_size"]
        lines.append(
            f"| {arm} | {m['fit']:.2f} | {s['beat']:.2f} | {s['continue']:.2f} | {s['peak']:.2f} | "
            f"{100 * m['identify']:.0f}% | {m['rows']} |"
        )
    lines += ["", "| A/B | wins | wins | split (position-biased) | share of agreed |", "|---|---|---|---|---|"]
    for pair, w in metrics["pairs"].items():
        a, b = pair.split("-")
        lines.append(
            f"| {a} vs {b} | {a} {w[a]} | {b} {w[b]} | {w['split']} | {100 * w[f'{a}_share_of_agreed']:.0f}% |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="eval.speech", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--val", type=Path, default=VAL)
    ap.add_argument("--arm", action="append", default=[], help="name=answers.json (ml.distill.speech_eval --out)")
    ap.add_argument("--pair", action="append", default=[], help="a,b: A/B the two arms")
    ap.add_argument("--judge", choices=["openai", "gemini"], default="openai")
    ap.add_argument("--model")
    ap.add_argument("--n", type=int, default=2, help="fit ratings per clip (A/B rounds = n / 2)")
    ap.add_argument("--rows", type=int, help="judge only this many rows (a deterministic sample of whole replies)")
    ap.add_argument("--workers", type=int, default=4, help="video render processes")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    load_env()
    rows = [json.loads(line) for line in a.val.read_text().splitlines() if line.strip()]
    if a.rows:
        replies = sorted({r["reply"] for r in rows}, key=lambda i: zlib.crc32(i.encode()))
        keep, count = set(), 0
        for reply in replies:
            if count >= a.rows:
                break
            keep.add(reply)
            count += sum(r["reply"] == reply for r in rows)
        rows = [r for r in rows if r["reply"] in keep]
    arms = load_arms(a.arm, rows)
    pairs = [(x, y) for x, _, y in (p.partition(",") for p in a.pair)]
    kind: JudgeKind = "gemini" if a.judge == "gemini" else "openai"
    media = build_media(a.out, rows, arms, videos=kind == "gemini", workers=a.workers)
    root = judge_all(a.out, Judge(kind, a.model), rows, media, pairs, a.n)
    metrics = report(root, rows, list(arms), pairs)
    _write(root / "metrics.json", metrics)
    (root / "report.md").write_text(markdown(metrics))
    print(markdown(metrics))


if __name__ == "__main__":
    main()
