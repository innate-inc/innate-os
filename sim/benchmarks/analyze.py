"""Score the benchmark runs and, more to the point, test whether the policy
did what it was TOLD.

Overall success says how good the policy is. The paired test says whether the
instruction mattered: scenarios sharing a spawn differ only in the words, so
for each run we ask whether the robot finished nearer the goal it was sent to
than the goal the OTHER instruction from that same pose named. Chance is 50%.
"""
import glob, json, math, os, sys
from math import comb
from collections import defaultdict

def load(path):
    d = json.load(open(path))
    return d["label"], [r for r in d["results"] if "final_dist_m" in r]

def paired(results):
    """Per spawn, compare distance-to-own-goal against distance-to-sibling-goal."""
    by_spawn = defaultdict(list)
    for r in results:
        by_spawn[tuple(r["spawn"])].append(r)
    wins = comps = 0
    per_turn = defaultdict(lambda: [0, 0])
    for spawn, group in by_spawn.items():
        if len(group) < 2: continue
        for r in group:
            gx, gy = r["goal"]
            for other in group:
                if other is r or other["goal"] == r["goal"]: continue
                ox, oy = other["goal"]
                # where the robot actually finished
                fx = r["final_dist_m"]
                # distance from its finish to the sibling goal: recover the
                # finish point from the goal and the recorded distance is not
                # possible, so use the travelled endpoint stored per run
                if "final_xy" not in r: continue
                ex, ey = r["final_xy"]
                own = math.hypot(ex - gx, ey - gy)
                alt = math.hypot(ex - ox, ey - oy)
                comps += 1
                per_turn[r["turn"]][1] += 1
                if own < alt:
                    wins += 1
                    per_turn[r["turn"]][0] += 1
    return wins, comps, per_turn

def binom_p(wins, n):
    """Two-sided probability of a split at least this lopsided under chance."""
    if not n: return 1.0
    tail = sum(comb(n, k) for k in range(0, min(wins, n - wins) + 1)) / 2**n
    return min(1.0, 2 * tail)


rows = []
for path in sorted(glob.glob(sys.argv[1] if len(sys.argv) > 1 else "results/*.json")):
    label, res = load(path)
    if not res: continue
    n = len(res)
    # The closed-loop harness counts success inside 3 m (VLN-CE convention).
    # That is most of this apartment, so report a tight and a loose radius too.
    at = {rad: sum(1 for r in res if r["final_dist_m"] <= rad) for rad in (0.75, 1.5, 3.0)}
    ok = sum(r["success"] for r in res)
    orc = sum(r["oracle"] for r in res)
    finals = sorted(r["final_dist_m"] for r in res)
    starts = [math.dist(r["spawn"][:2], r["goal"]) for r in res]
    # did it get closer than just sitting at the spawn?
    improved = sum(1 for r, s in zip(res, starts) if r["final_dist_m"] < s - 0.25)
    timeouts = sum(1 for r in res if r["outcome"] == "timeout")
    w, c, per_turn = paired(res)
    rows.append(dict(label=label, n=n, ok=ok, orc=orc, at=at,
                     med=finals[len(finals)//2], timeouts=timeouts,
                     improved=improved, wins=w, comps=c, per_turn=dict(per_turn),
                     travelled=sum(r.get("travelled_m", 0) for r in res)/max(n,1)))

print(f"{'checkpoint':24} {'n':>3} {'<0.75m':>7} {'<1.5m':>7} {'<3m':>7} {'oracle':>7} "
      f"{'closer':>7} {'med err':>9} {'timeout':>8} {'drove':>7}")
for r in rows:
    print(f"{r['label']:24} {r['n']:>3} {r['at'][0.75]:>7} {r['at'][1.5]:>7} {r['at'][3.0]:>7} "
          f"{r['orc']:>7} {r['improved']:>7} {r['med']:>7.2f} m {r['timeouts']:>8} {r['travelled']:>5.1f} m")

print("\ninstruction-following (finished nearer its own goal than the sibling goal):")
for r in rows:
    if not r["comps"]:
        print(f"  {r['label']:26} no paired comparisons"); continue
    p = binom_p(r['wins'], r['comps'])
    verdict = "indistinguishable from chance" if p > 0.05 else "above chance"
    print(f"  {r['label']:24} {r['wins']:>3}/{r['comps']:<3} = {r['wins']/r['comps']:.0%}  "
          f"p={p:.3f}  {verdict}")
    for turn, (w, c) in sorted(r["per_turn"].items()):
        if c: print(f"      {turn:9} {w}/{c}")
