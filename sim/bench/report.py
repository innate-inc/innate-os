#!/usr/bin/env python3
"""Merge per-map result files into one report.

WHY THIS EXISTS RATHER THAN JUST RUNNING EVERY MAP AT ONCE. A whole-suite sweep
takes long enough that it keeps getting reaped by the environment it runs in --
WSL tears down background processes when the session that started them ends, and
a 42-episode run does not fit in one foreground call. Per-map runs each finish
in under two minutes and always complete.

That is a property of THIS machine, not of the benchmark, and the fix is not to
make the sweep shorter. It is to make the sweep resumable, which it now is:
every map writes its own results file, and this stitches them together.

  ./sim/.venv/bin/python sim/bench/main.py --map counter --out sim/bench/results/bench_counter.json
  ...
  ./sim/.venv/bin/python sim/bench/report.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ros2_ws/src/mars_bot/mars_sim_driver"))

from bench_common import (  # noqa: E402
    CATEGORY_NAMES,
    CATEGORY_ORDER,
    VERDICTS,
    format_scorecard,
    gate_verdict,
    scorecard,
)

RESULTS = Path(__file__).resolve().parent / "results"


def main() -> int:
    files = sorted(RESULTS.glob("bench_*.json"))
    # main.py's default --out is bench_results.json, which the glob also
    # matches -- so a default-out sweep re-running challenges already saved
    # per-map would count every episode twice, inflating numerator and
    # denominator together. When per-map files exist, the default file is
    # skipped loudly; when it is all there is, it is used as-is.
    per_map = [f for f in files if f.name != "bench_results.json"]
    if per_map and len(per_map) != len(files):
        print("skipping bench_results.json (main.py's default --out): per-map files cover the same challenges")
        files = per_map
    if not files:
        print(f"no bench_*.json in {RESULTS}")
        return 1

    rows = []
    for f in files:
        try:
            rows.extend(json.loads(f.read_text()))
        except Exception as exc:  # noqa: BLE001
            print(f"skipping {f.name}: {exc}")

    # Categories come from the challenge files, not from the results, so a
    # results file written before categories existed still reports correctly.
    from mars_sim_driver.challenges import load_challenges
    from runner import sources

    cat = {}
    for _name, (_assets, root) in sources().items():
        for cid, ch in load_challenges([root]).items():
            cat[cid] = ch.category

    # --- gate ---------------------------------------------------------------
    by_ch: dict[str, list] = {}
    for r in rows:
        by_ch.setdefault(r["challenge"], []).append(r)

    tally = dict.fromkeys(VERDICTS, 0)
    valid: set[str] = set()
    flagged: list[tuple[str, str, str]] = []
    for cid, eps in by_ch.items():
        oracle = next((e for e in eps if e["agent"] == "oracle"), None)
        rnd = [e for e in eps if e["agent"] == "random"]
        # autoplan's classification rides every row main.py writes (Episode.needs).
        req = next((e.get("needs", "") for e in eps if e.get("needs")), "")
        verdict, why = gate_verdict(req, oracle, rnd)
        tally[verdict] += 1
        if verdict == "VALID":
            valid.add(cid)
        else:
            flagged.append((cid, verdict, why))

    print(f"=== {len(by_ch)} challenges from {len(files)} map file(s) ===")
    print("  " + "   ".join(f"{k} {v}" for k, v in tally.items()))
    for cid, verdict, why in sorted(flagged):
        print(f"    {verdict:<10} {cid:<28} {why}")

    # --- per-category, per-agent -------------------------------------------
    for agent in sorted({r["agent"] for r in rows} - {"random"}):
        card = scorecard(rows, cat, valid, agent)
        if card is None:
            continue
        print(f"\n=== {agent} ===")
        for line in format_scorecard(*card):
            print(line)
        # A blind episode is not a result. Say so loudly and next to the score
        # it would otherwise silently depress -- the whole point of counting
        # camera failures was that somebody sees the count.
        eps_all = [e for e in rows if e["agent"] == agent and e["challenge"] in valid]
        blind = [e for e in eps_all if e.get("camera_errors", 0)]
        if blind:
            print(f"  !! {len(blind)} episode(s) had CAMERA FAILURES -- those scores are not perception results:")
            for e in sorted(blind, key=lambda x: x["challenge"])[:6]:
                print(f"       {e['challenge']:<28} {e['camera_errors']} failed frame(s)")

    # --- the suite's own shape ---------------------------------------------
    print("\n=== suite ===")
    for c in CATEGORY_ORDER:
        ids = sorted(cid for cid in by_ch if cat.get(cid, 0) == c)
        if ids:
            print(f"  {CATEGORY_NAMES[c]:<38} {len(ids):>2}   {', '.join(ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
