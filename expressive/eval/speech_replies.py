"""Whole replies, judged as the person would see them: each held-out reply spoken back to back (macOS voice,
speech-led timing: a sentence's clip starts with it and replaces the last one) with every arm's recipes, filmed, and
watched by Gemini with the sound on. A single line can look better busy; a reply shows whether the body stays natural.

  uv run python -m eval.speech_replies --arm talk4b=talk4b.json --arm s4=s4.json --pair talk4b,s4 \\
      --replies 24 --out out/speech_replies

rate   per video: fit 1-5 (the body matches the words, line by line), natural 1-5 (1 = a new big gesture on every
       sentence, frantic; 5 = calm beats on plain lines and bigger moments only where the words call for them),
       expressive 1-5
ab     per reply and pair of arms, both orders; a win counts only when both orders agree
Videos show the spoken line as a subtitle and what the person said as the title; never the arm, recipe or idea.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import _core  # noqa: F401
import numpy as np
import yaml

from eval.llm import Json, Proxy, load_env
from eval.run import _parallel, _read, _write
from eval.speech import VAL, load_arms

logger = logging.getLogger("mars-express.speech-replies")
MODEL = "gemini-3.1-pro-preview"
CONTEXT = (
    "The video shows MARS, a small robot in a simulator (a dark wheeled base, one yellow arm ending in a black claw "
    "gripper, its only limb, and a flat camera head that only tilts up or down), speaking one reply out loud: its voice "
    "is the audio and the subtitle shows each sentence as it is said; the title at the top shows what the person had "
    "just said to it, if anything. Watch the robot's body while it talks."
)
RATE_TASK = (
    "Rate the body language over the whole reply, each 1-5:\n"
    "- fit: how well the body matches what is being said, sentence by sentence (feeling, size, and physical cues the "
    "words call for, such as a nod, a head shake by turning the base, a wave, turning toward what it mentions).\n"
    "- natural: 1 = restless or frantic, a new big gesture on every sentence; 5 = natural conversational body "
    "language, calm small beats on plain sentences and bigger moments only where the words call for them.\n"
    "- expressive: 1 = a machine; 5 = clearly feels what it says.\n"
    "One sentence of reasoning first."
)
PAIR_TASK = (
    "You will see two videos, ONE then TWO, of the robot speaking the same reply with different body language. Which "
    "one's body language fits what it says better and reads as more natural (not frantic, not dead)? Give one "
    "sentence of reasoning, the winner, and whether the difference is clear or slight."
)
RATE_SCHEMA: Json = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "fit": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "natural": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "expressive": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
    },
    "required": ["reason", "fit", "natural", "expressive"],
}
PAIR_SCHEMA: Json = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
        "winner": {"type": "string", "enum": ["ONE", "TWO"]},
        "margin": {"type": "string", "enum": ["clear", "slight"]},
    },
    "required": ["reason", "winner", "margin"],
}
Row = dict[str, Any]


def pick_replies(rows: list[Row], count: int, min_sentences: int = 3) -> list[str]:
    """``count`` held-out replies of at least ``min_sentences``, one scene at a time (deterministic)."""
    by_scene: dict[str, list[str]] = {}
    for r in rows:
        if r["sentences"] >= min_sentences and r["pos"] == 0:
            by_scene.setdefault(r["scene"], []).append(r["reply"])
    ordered = [sorted(ids, key=lambda i: zlib.crc32(i.encode())) for ids in by_scene.values()]
    picked = [i for k in range(max(map(len, ordered))) for ids in ordered if k < len(ids) for i in [ids[k]]]
    return picked[:count]


def show_yaml(rows: list[Row], recipes: dict[str, str | None]) -> dict[str, Any]:
    heard = rows[0]["heard"]
    return {
        "title": f'They said: "{heard[:80]}"' if heard else "",
        "voice": "Samantha",
        "rate": 175,
        "intro": 1.0,
        "outro": 1.5,
        "speech_led": True,
        "beats": [
            {"say": r["sentence"], "recipe": recipes[r["id"]] or "hold 1 E=.8", "note": "gesture", "pause": 0.15}
            for r in rows
        ],
    }


def _render(job: tuple[str, str]) -> str:
    from demo.show import Show, Studio, render_show

    yaml_path, mp4 = job
    partial = Path(mp4).with_suffix(".partial.mp4")  # a killed render must not leave a playable half video in the cache
    render_show(Show.load(Path(yaml_path)), partial, Studio(Path(mp4).parent / "cache", planner_model=None))
    partial.replace(mp4)
    return mp4


def build_videos(
    out: Path, rows: list[Row], replies: list[str], arms: dict[str, dict[str, str | None]], workers: int
) -> None:
    from demo.show import Studio

    studio = Studio(out / "videos" / "cache", planner_model=None)
    jobs = []
    for reply in replies:
        mine = sorted((r for r in rows if r["reply"] == reply), key=lambda r: r["pos"])
        for r in mine:
            studio.speak(r["sentence"], "Samantha", 175)  # once, here: the render processes share this cache
        for arm, recipes in arms.items():
            yaml_path, mp4 = out / "shows" / f"{reply}.{arm}.yaml", out / "videos" / f"{reply}.{arm}.mp4"
            yaml_path.parent.mkdir(parents=True, exist_ok=True)
            mp4.parent.mkdir(parents=True, exist_ok=True)
            yaml_path.write_text(yaml.safe_dump(show_yaml(mine, recipes), sort_keys=False, allow_unicode=True))
            if not mp4.exists():
                jobs.append((str(yaml_path), str(mp4)))
    logger.info("rendering %d reply videos", len(jobs))
    with ProcessPoolExecutor(workers) as pool:
        for k, _ in enumerate(pool.map(_render, jobs), 1):
            if k % 10 == 0 or k == len(jobs):
                logger.info("video %d/%d", k, len(jobs))


def _video(path: Path) -> Json:
    return {"inlineData": {"mimeType": "video/mp4", "data": base64.b64encode(path.read_bytes()).decode()}}


def judge(out: Path, replies: list[str], arms: list[str], pairs: list[tuple[str, str]], n: int) -> None:
    proxy = Proxy()
    root = out / "judged" / MODEL

    def rate(job: tuple[str, str]) -> None:
        path = root / "rate" / f"{job[0]}.{job[1]}.json"
        done: list[Json] = _read(path) or []
        while len(done) < n:
            video = out / "videos" / f"{job[0]}.{job[1]}.mp4"
            done.append(
                proxy.gemini_json(MODEL, [{"text": f"{CONTEXT}\n\n{RATE_TASK}"}, _video(video)], RATE_SCHEMA, 0.7)
            )
            _write(path, done)

    def pair(job: tuple[str, tuple[str, str]]) -> None:
        reply, (a, b) = job
        path = root / "ab" / f"{reply}.{a}-{b}.json"
        rounds: list[list[str]] = _read(path) or []
        while len(rounds) < max(1, n):
            verdicts = []
            for first, second in ((a, b), (b, a)):
                parts = [
                    {"text": f"{CONTEXT}\n\n{PAIR_TASK}"},
                    {"text": "Video ONE:"},
                    _video(out / "videos" / f"{reply}.{first}.mp4"),
                    {"text": "Video TWO:"},
                    _video(out / "videos" / f"{reply}.{second}.mp4"),
                ]
                raw = proxy.gemini_json(MODEL, parts, PAIR_SCHEMA, 0.3)
                verdicts.append(first if raw.get("winner") == "ONE" else second)
            rounds.append(verdicts)
            _write(path, rounds)

    _parallel([(r, a) for r in replies for a in arms], rate, 8, "rate")
    _parallel([(r, p) for r in replies for p in pairs], pair, 8, "pair")


def report(out: Path, replies: list[str], arms: list[str], pairs: list[tuple[str, str]]) -> str:
    root = out / "judged" / MODEL
    lines = ["| arm | fit 1-5 | natural 1-5 | expressive 1-5 | videos |", "|---|---|---|---|---|"]
    metrics: dict[str, Any] = {"arms": {}, "pairs": {}}
    for arm in arms:
        got = [x for r in replies for x in _read(root / "rate" / f"{r}.{arm}.json") or []]
        m = {k: float(np.mean([x[k] for x in got])) if got else float("nan") for k in ("fit", "natural", "expressive")}
        metrics["arms"][arm] = m | {"ratings": len(got)}
        lines.append(f"| {arm} | {m['fit']:.2f} | {m['natural']:.2f} | {m['expressive']:.2f} | {len(got)} |")
    lines += ["", "| A/B (replies) | wins | wins | split | share of agreed |", "|---|---|---|---|---|"]
    for a, b in pairs:
        wins = {a: 0, b: 0, "split": 0}
        for r in replies:
            for first, second in _read(root / "ab" / f"{r}.{a}-{b}.json") or []:
                wins[first if first == second else "split"] += 1
        agreed = wins[a] + wins[b]
        metrics["pairs"][f"{a}-{b}"] = wins
        share = f"{100 * wins[a] / agreed:.0f}%" if agreed else "-"
        lines.append(f"| {a} vs {b} | {a} {wins[a]} | {b} {wins[b]} | {wins['split']} | {share} |")
    _write(root / "metrics.json", metrics)
    text = "\n".join(lines) + "\n"
    (root / "report.md").write_text(text)
    return text


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="eval.speech_replies", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--val", type=Path, default=VAL)
    ap.add_argument("--arm", action="append", default=[], help="name=answers.json (ml.distill.speech_eval --out)")
    ap.add_argument("--pair", action="append", default=[], help="a,b")
    ap.add_argument("--replies", type=int, default=24)
    ap.add_argument("--n", type=int, default=1, help="ratings per video and A/B rounds per pair")
    ap.add_argument("--workers", type=int, default=6, help="render processes")
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    load_env()
    rows = [json.loads(line) for line in a.val.read_text().splitlines() if line.strip()]
    replies = pick_replies(rows, a.replies)
    arms = load_arms(a.arm, rows)
    pairs = [(x, y) for x, _, y in (p.partition(",") for p in a.pair)]
    build_videos(a.out, rows, replies, arms, a.workers)
    judge(a.out, replies, list(arms), pairs, a.n)
    print(report(a.out, replies, list(arms), pairs))


if __name__ == "__main__":
    main()
