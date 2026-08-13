"""Retrieval benchmark: run the production spatial-memory search over a
captured dataset and score it against ground truth.

This drives brain_client's real MemorySearch/MemoryStore/GeminiRest — the
exact code path the robot uses, including the context-cache tiers — so what
is measured here (accuracy, latency, cached vs inline) is what ships.

    uv sync --group benchmark
    GEMINI_API_KEY=... uv run benchmarks/spatial_memory/run_benchmark.py \
        --models gemini-3.6-flash,gemini-3.6-pro

Modes:
    --cache / --no-cache   warm the Gemini context cache first (production
                           steady state) vs inline frames on every call
    --fake oracle|first    no network: oracle answers from gold (validates
                           scoring = 1.0), first always picks frame 1
Each model runs its tasks sequentially (sharing its cache); models run in
parallel threads. Writes results.json + report.md under --out.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import _paths  # noqa: F401
from apartment import room_at

from brain_client.brain.memory_search import MemorySearch, SearchVerdict
from brain_client.brain.transport import direct_rest
from brain_client.memory.store import MemoryStore

MAP_NAME = "sim_apartment.yaml"
HERE = Path(__file__).resolve().parent


class PrintLogger:
    """Duck-types the rclpy logger surface MemorySearch expects."""

    def _log(self, level: str, msg: str) -> None:
        print(f"[{level}] {msg}")

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        self._log("info", msg)

    def warning(self, msg: str) -> None:
        self._log("warn", msg)

    warn = warning

    def error(self, msg: str) -> None:
        self._log("error", msg)


def load_env_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key:
        return key
    for env_file in (_paths.SIM_DIR / ".env", _paths.REPO_ROOT / ".env"):
        if not env_file.is_file():
            continue
        for line in env_file.read_text().splitlines():
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    return ""


def build_query(task: dict, style: str) -> str:
    query = task.get("query") or task["prompt"]
    if style == "context" and task.get("context"):
        return f"{task['context']} User request: {query}"
    return query


def score(task: dict, verdict: SearchVerdict, visibility: dict) -> tuple[float, str]:
    if verdict.error:
        return 0.0, f"error: {verdict.error}"  # a transport failure is not an honest no-match
    gold = task["gold"]
    if gold.get("none"):
        return (1.0, "honest no-match") if not verdict.found else (0.0, "hallucinated a match")
    if not verdict.found or verdict.memory is None:
        return 0.0, "missed"
    mem = verdict.memory
    seen = set(visibility.get("memories", {}).get(str(mem.id), {}))
    if gold.get("props") and seen & set(gold["props"]):
        return 1.0, f"frame {mem.id} shows {sorted(seen & set(gold['props']))}"
    region = gold.get("region")
    if region and math.hypot(mem.x - region["x"], mem.y - region["y"]) <= region["r"]:
        return 1.0, f"frame {mem.id} within {region['r']}m of target"
    room = room_at(mem.x, mem.y)
    if room in gold.get("rooms", ()):
        partial = 0.5 if gold.get("props") else 1.0
        return partial, f"frame {mem.id} in {room} (right room{', wrong frame for the object' if partial < 1 else ''})"
    return 0.0, f"frame {mem.id} in {room or 'nowhere'} — not a gold room"


def fake_search(kind: str, task: dict, store: MemoryStore, visibility: dict):
    memories = store.snapshot().memories
    gold = task["gold"]
    if kind == "first":
        m = memories[0]
        return SearchVerdict(query=task["prompt"], found=True, memory=m, explanation="first frame", cached=True)
    if gold.get("none"):
        return SearchVerdict(query=task["prompt"], found=False, explanation="nothing matches")
    for m in memories:
        seen = set(visibility.get("memories", {}).get(str(m.id), {}))
        if gold.get("props") and seen & set(gold["props"]):
            return SearchVerdict(query=task["prompt"], found=True, memory=m, explanation="oracle prop", cached=True)
    for m in memories:
        if room_at(m.x, m.y) in gold.get("rooms", ()):
            return SearchVerdict(query=task["prompt"], found=True, memory=m, explanation="oracle room", cached=True)
    region = gold.get("region")
    if region and memories:
        m = min(memories, key=lambda m: math.hypot(m.x - region["x"], m.y - region["y"]))
        return SearchVerdict(query=task["prompt"], found=True, memory=m, explanation="oracle region", cached=True)
    return SearchVerdict(query=task["prompt"], found=False, explanation="oracle: no gold reachable")


def wait_for_cache(search: MemorySearch, store: MemoryStore, timeout: float = 240.0) -> str:
    store.last_change_monotonic = time.monotonic() - 60.0  # the tour is over; skip the quiet window
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        search.warm()
        state = search.cache_state()
        if state in ("warm", "inline", "unsupported"):
            return state
        time.sleep(2.0)
    return search.cache_state()


def run_model(model: str, args, tasks: list[dict], visibility: dict, api_key: str) -> dict:
    store = MemoryStore(args.dataset / "data")
    store.switch_map(MAP_NAME)
    n = len(store.snapshot().memories)
    searcher = None
    cache_state = "off"
    if not args.fake:
        searcher = MemorySearch(store, direct_rest(api_key), model=model, logger=PrintLogger())
        if args.cache:
            t0 = time.monotonic()
            cache_state = wait_for_cache(searcher, store)
            print(f"[{model}] cache: {cache_state} after {time.monotonic() - t0:.0f}s ({n} frames)")
        else:
            cache_state = "inline-forced"

    rows = []
    for task in tasks:
        query = build_query(task, args.query_style)
        if args.fake:
            verdict = fake_search(args.fake, task, store, visibility)
        else:
            assert searcher is not None
            verdict = searcher.search(query)
        points, reason = score(task, verdict, visibility)
        rows.append(
            {
                "task": task["id"],
                "category": task["category"],
                "query": query,
                "score": points,
                "reason": reason,
                "found": verdict.found,
                "frame": verdict.memory.id if verdict.memory else None,
                "pose": [round(verdict.memory.x, 2), round(verdict.memory.y, 2)] if verdict.memory else None,
                "room": room_at(verdict.memory.x, verdict.memory.y) if verdict.memory else None,
                "explanation": verdict.explanation,
                "error": verdict.error,
                "latency_sec": round(verdict.latency_sec, 2),
                "cached": verdict.cached,
            }
        )
        print(f"[{model}] {task['id']}: {points:.1f} ({reason}) {verdict.latency_sec:.1f}s")
    return {"model": model, "cache_state": cache_state, "frames": n, "rows": rows}


def summarize(result: dict) -> dict:
    rows = result["rows"]
    by_cat: dict[str, list[float]] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r["score"])
    lat = [r["latency_sec"] for r in rows if r["latency_sec"] > 0]
    return {
        "model": result["model"],
        "cache_state": result["cache_state"],
        "overall": round(sum(r["score"] for r in rows) / len(rows), 3),
        "by_category": {c: round(sum(v) / len(v), 3) for c, v in sorted(by_cat.items())},
        "mean_latency_sec": round(sum(lat) / len(lat), 2) if lat else None,
        "cached_fraction": round(sum(1 for r in rows if r["cached"]) / len(rows), 2),
    }


def write_report(out: Path, results: list[dict], meta: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps({"meta": meta, "results": results}, indent=1))
    lines = ["# Spatial-memory retrieval benchmark", ""]
    lines.append(
        f"dataset: `{meta['dataset']}` ({meta['frames']} memories) — mode: {meta['mode']}, query style: {meta['query_style']}"
    )
    lines.append("")
    lines.append(
        "| model | overall | "
        + " | ".join(sorted({c for r in results for c in summarize(r)["by_category"]}))
        + " | mean latency |"
    )
    cats = sorted({c for r in results for c in summarize(r)["by_category"]})
    lines.append("|" + "---|" * (len(cats) + 3))
    for r in results:
        s = summarize(r)
        row = [s["model"], f"{s['overall']:.2f}"] + [f"{s['by_category'].get(c, 0):.2f}" for c in cats]
        row.append(f"{s['mean_latency_sec']}s" if s["mean_latency_sec"] else "-")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    for r in results:
        lines.append(f"## {r['model']} (cache: {r['cache_state']})")
        lines.append("")
        lines.append("| task | score | frame | room | latency | reason |")
        lines.append("|---|---|---|---|---|---|")
        for row in r["rows"]:
            lines.append(
                f"| {row['task']} | {row['score']:.1f} | {row['frame'] or '-'} | {row['room'] or '-'} "
                f"| {row['latency_sec']}s | {row['reason']} |"
            )
        lines.append("")
    (out / "report.md").write_text("\n".join(lines))
    print(f"report at {out / 'report.md'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", type=Path, default=HERE / "dataset/home_tour")
    ap.add_argument("--tasks", type=Path, default=HERE / "tasks.json")
    ap.add_argument("--models", default="gemini-3.6-flash")
    ap.add_argument("--cache", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--query-style", choices=("raw", "context"), default="context")
    ap.add_argument("--fake", choices=("oracle", "first"), help="offline scripted retrieval (no API)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    args.dataset = args.dataset.resolve()

    tasks = json.loads(args.tasks.read_text())["tasks"]
    visibility = json.loads((args.dataset / "visibility.json").read_text())
    api_key = load_env_key()
    if not args.fake and not api_key:
        raise SystemExit("GEMINI_API_KEY not set (env or .env) — use --fake oracle to test the harness offline")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if args.fake:
        models = [f"fake-{args.fake}"]

    with ThreadPoolExecutor(max_workers=len(models)) as pool:
        results = list(pool.map(lambda m: run_model(m, args, tasks, visibility, api_key), models))

    mode = args.fake or ("cached" if args.cache else "inline")
    out = args.out or HERE / "results" / f"{time.strftime('%Y%m%d-%H%M%S')}-{mode}"
    meta = {
        "dataset": str(args.dataset),
        "frames": results[0]["frames"],
        "mode": mode,
        "query_style": args.query_style,
        "tasks": len(tasks),
    }
    write_report(out, results, meta)
    for r in results:
        print(json.dumps(summarize(r)))


if __name__ == "__main__":
    main()
