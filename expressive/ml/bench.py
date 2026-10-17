"""End-to-end latency of a running planner service: 16 prompts (or ``--prompts`` jsonl), n = 1, wall time per tier;
``--concurrency N`` keeps N requests in flight (N robots or a robot plus the studio sharing the server).

python -m ml.bench [--url http://innate52.local:8000] [--effort medium low] [--n 1] [--sparse]
python -m ml.bench --url http://192.168.0.156:8001 --prompts ml/distill_data/speech_val.jsonl --concurrency 1 4 8
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

PROMPTS = (
    "happy. A friend just walked into the room.",
    "sad. Your favourite toy is broken.",
    "curious. A strange box appeared on the floor.",
    "proud. You finally solved the puzzle.",
    "greeting someone who just walked in.",
    "listening closely to a question.",
    "confused. That instruction makes no sense.",
    "apologising. You knocked over a glass.",
    "celebrating a completed task.",
    "failing a task. The object slipped again.",
    "a dog waiting for a treat.",
    "low battery. You are running out of energy.",
    "hammering a nail. Bang, bang, bang.",
    "saying goodbye to a friend.",
    "being ignored. Nobody looks at you.",
    "excited. You are about to go to the park.",
)


def post(url: str, body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, json.dumps(body).encode(), {"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def bench(
    url: str, effort: str, n: int, sparse: bool, prompts: tuple[str, ...] = PROMPTS, concurrency: int = 1
) -> dict[str, float]:
    endpoint = f"{url}/generate-{'sparse' if sparse else 'dense'}"
    post(endpoint, {"prompt": prompts[0], "n": n, "effort": effort})  # warm-up

    def one(job: tuple[int, str]) -> tuple[float, dict[str, Any]]:
        start = time.perf_counter()
        result = post(endpoint, {"prompt": job[1], "n": n, "seed": job[0], "effort": effort})
        return 1e3 * (time.perf_counter() - start), result

    start = time.perf_counter()
    with ThreadPoolExecutor(concurrency) as pool:
        done = list(pool.map(one, enumerate(prompts)))
    seconds = time.perf_counter() - start
    walls = [wall for wall, _ in done]
    planner = [result["timing_ms"]["planner"] for _, result in done]
    generator = [result["timing_ms"].get("generator", 0) for _, result in done]
    report = {
        "concurrency": concurrency,
        "requests_per_s": len(prompts) / seconds,
        "wall_median_ms": statistics.median(walls),
        "wall_p90_ms": float(np.percentile(walls, 90)),
        "wall_p95_ms": float(np.percentile(walls, 95)),
        "planner_median_ms": statistics.median(planner),
        "planner_p95_ms": float(np.percentile(planner, 95)),
        "generator_median_ms": statistics.median(generator),
    }
    print(
        f"{effort:6s} x{concurrency} {len(walls)} prompts n={n}: {report['requests_per_s']:.1f} req/s, wall median {report['wall_median_ms']:.0f} ms, p90 "
        f"{report['wall_p90_ms']:.0f} ms, p95 {report['wall_p95_ms']:.0f} ms | planner "
        f"{report['planner_median_ms']:.0f} ms (p95 {report['planner_p95_ms']:.0f}), generator "
        f"{report['generator_median_ms']:.0f} ms"
    )
    return report


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="ml.bench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="http://localhost:8000")
    ap.add_argument("--effort", nargs="+", default=["medium", "low"])
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--sparse", action="store_true")
    ap.add_argument("--prompts", type=Path, help="jsonl rows with a 'prompt' (e.g. distill_data/speech_val.jsonl)")
    ap.add_argument("--concurrency", type=int, nargs="+", default=[1], help="requests kept in flight (one run each)")
    ap.add_argument("--out", type=Path, help="append the reports to this json file")
    a = ap.parse_args()
    prompts = (
        tuple(json.loads(line)["prompt"] for line in a.prompts.read_text().splitlines() if line.strip())
        if a.prompts
        else PROMPTS
    )
    reports = [bench(a.url, e, a.n, a.sparse, prompts, c) | {"effort": e} for e in a.effort for c in a.concurrency]
    if a.out:
        old = json.loads(a.out.read_text()) if a.out.exists() else []
        a.out.write_text(json.dumps(old + reports, indent=1))


if __name__ == "__main__":
    main()
