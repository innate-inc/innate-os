"""Per-family scoring, plus the check pointnav makes possible.

pointnav states the goal in the robot's own frame, so the direction it was told
to go is known exactly. Comparing that against the direction it actually moved
separates "understood the instruction and failed to execute" from "did not use
the instruction at all" — which success rate alone cannot do.
"""
import glob, json, math, statistics, sys
from collections import defaultdict

FAMILIES = ("pointnav", "objectnav", "r2r")

def bearing_err(r):
    """Angle between the commanded goal direction and the net displacement."""
    sx, sy, _ = r["spawn"]
    ex, ey = r["final_xy"]
    gx, gy = min(r["goals"], key=lambda g: math.dist(g, (ex, ey)))
    if math.dist((ex, ey), (sx, sy)) < 0.4: return None      # never really moved
    want = math.atan2(gy - sy, gx - sx)
    got = math.atan2(ey - sy, ex - sx)
    return abs(math.degrees(math.atan2(math.sin(got - want), math.cos(got - want))))

for path in sorted(glob.glob(sys.argv[1])):
    doc = json.load(open(path))
    res = [r for r in doc["results"] if "final_xy" in r]
    if not res: continue
    print(f"\n=== {doc['label']} ===")
    print(f"{'family':10} {'n':>3} {'<0.75m':>7} {'<1.5m':>7} {'oracle':>7} {'med err':>9} "
          f"{'start':>8} {'closed':>8} {'timeout':>8}")
    by = defaultdict(list)
    for r in res: by[r.get("family", "r2r")].append(r)
    for fam in FAMILIES:
        g = by.get(fam) or []
        if not g: continue
        start = [min(math.dist(r["spawn"][:2], gl) for gl in r["goals"]) for r in g]
        closed = [s - r["final_dist_m"] for r, s in zip(g, start)]
        print(f"{fam:10} {len(g):>3} "
              f"{sum(r['final_dist_m'] <= 0.75 for r in g):>7} "
              f"{sum(r['final_dist_m'] <= 1.5 for r in g):>7} "
              f"{sum(r['oracle'] for r in g):>7} "
              f"{statistics.median(r['final_dist_m'] for r in g):>7.2f} m "
              f"{statistics.median(start):>6.2f} m "
              f"{statistics.median(closed):>+6.2f} m "
              f"{sum(r['outcome'] == 'timeout' for r in g):>8}")
    pn = by.get("pointnav") or []
    errs = [e for e in (bearing_err(r) for r in pn) if e is not None]
    if errs:
        errs.sort()
        within = sum(1 for e in errs if e <= 45)
        print(f"\npointnav heading check ({len(errs)} runs that moved):")
        print(f"  median error between commanded and actual direction: {errs[len(errs)//2]:.0f}°")
        print(f"  went roughly the right way (within 45°): {within}/{len(errs)} "
              f"({within/len(errs):.0%}; a random heading gives 25%)")
