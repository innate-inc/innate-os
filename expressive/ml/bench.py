"""End-to-end latency of a running planner service: 16 prompts, n = 1, median / p90 wall time per effort tier.

python -m ml.bench [--url http://innate52.local:8000] [--effort medium low] [--n 1] [--sparse]
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.request
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


def bench(url: str, effort: str, n: int, sparse: bool) -> dict[str, float]:
    endpoint = f"{url}/generate-{'sparse' if sparse else 'dense'}"
    walls, planner, generator = [], [], []
    for i, prompt in enumerate(PROMPTS):
        start = time.perf_counter()
        result = post(endpoint, {"prompt": prompt, "n": n, "seed": i, "effort": effort})
        walls.append(1e3 * (time.perf_counter() - start))
        planner.append(result["timing_ms"]["planner"])
        generator.append(result["timing_ms"].get("generator", 0))
    report = {
        "wall_median_ms": statistics.median(walls),
        "wall_p90_ms": float(np.percentile(walls, 90)),
        "planner_median_ms": statistics.median(planner),
        "generator_median_ms": statistics.median(generator),
    }
    print(
        f"{effort:6s} {len(walls)} prompts n={n}: wall median {report['wall_median_ms']:.0f} ms, p90 "
        f"{report['wall_p90_ms']:.0f} ms | planner {report['planner_median_ms']:.0f} ms, generator "
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
    a = ap.parse_args()
    for effort in a.effort:
        bench(a.url, effort, a.n, a.sparse)


if __name__ == "__main__":
    main()
