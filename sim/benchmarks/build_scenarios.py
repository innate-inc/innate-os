"""Turn the validated pairs into scenarios: same spawn, different instructions."""
import json, math
import cv2, numpy as np

RES, OX, OY = 0.05, -11.0884, -7.4106
GOAL_RADIUS, MIN_PATH, MAX_PATH = 0.75, 1.5, 6.0
m = cv2.imread('/tmp/map.pgm', cv2.IMREAD_GRAYSCALE); H, W = m.shape
dist = cv2.distanceTransform((m > 250).astype(np.uint8), cv2.DIST_L2, 5) * RES
reach = np.load('/tmp/bench/reach.npy')
cand = [tuple(p) for p in json.load(open('/tmp/bench/pairs.json'))["cand"]]
K8 = np.ones((3, 3), np.uint8)

def cell(x, y): return int(round(H - (y - OY)/RES)), int(round((x - OX)/RES))
def world(r, c): return c*RES + OX, (H - r)*RES + OY

def bfs(start):
    d = np.full((H, W), np.inf, np.float32); front = np.zeros((H, W), np.uint8)
    front[start] = 1; d[start] = 0.0; step = 0
    while front.any():
        step += 1
        grown = cv2.dilate(front, K8).astype(bool) & reach & np.isinf(d)
        d[grown] = step * RES; front = grown.astype(np.uint8)
    return d

dmap = {p: bfs(cell(*p)) for p in cand}

def first_bearing(s, g, lead=1.0):
    """Heading of the first metre of the actual route, not the straight line —
    corridors bend, and the instruction has to describe the turn the robot
    really makes."""
    d = dmap[g]; r, c = cell(*s); trav = 0.0
    while trav < lead:
        best = None
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                nr, nc = r+dr, c+dc
                if (dr or dc) and 0 <= nr < H and 0 <= nc < W and reach[nr, nc]:
                    if best is None or d[nr, nc] < d[best[0], best[1]]: best = (nr, nc)
        if best is None or d[best[0], best[1]] >= d[r, c]: break
        trav += RES * (1.414 if best[0] != r and best[1] != c else 1.0)
        r, c = best
    gx, gy = world(r, c)
    return math.degrees(math.atan2(gy - s[1], gx - s[0]))

def wrap(a): return (a + 180) % 360 - 180

TURNS = {
    "straight": ["Go straight ahead and stop at the end of the way.",
                 "Drive straight forward and stop when you get there.",
                 "Continue straight ahead."],
    "left":     ["Turn left and drive to the end.",
                 "Turn left and follow the passage.",
                 "Go left and keep going until you have to stop."],
    "right":    ["Turn right and drive to the end.",
                 "Turn right and follow the passage.",
                 "Go right and keep going until you have to stop."],
    "back":     ["Turn around and drive back the way you came.",
                 "Turn around and go to the far end behind you.",
                 "Turn around and keep going."],
}
def turn_class(a):
    a = abs(wrap(a))
    return "straight" if a <= 25 else ("back" if a >= 135 else None)

scenarios, sid = [], 0
for s in cand:
    goals = []
    for g in cand:
        if g == s: continue
        r, c = cell(*g); d = float(dmap[s][r, c])
        if MIN_PATH <= d <= MAX_PATH: goals.append((g, d, first_bearing(s, g)))
    if len(goals) < 2: continue
    # face the first goal: everything else is then a real turn away from it
    goals.sort(key=lambda t: -t[1])
    heading = goals[0][2]
    picked = {}
    for g, d, b in goals:
        rel = wrap(b - heading)
        cls = turn_class(rel) or ("left" if rel > 0 else "right")
        if cls not in picked: picked[cls] = (g, d, rel)
    if len(picked) < 2: continue
    for cls, (g, d, rel) in sorted(picked.items()):
        scenarios.append(dict(
            id=f"s{sid:02d}", spawn=[round(s[0],2), round(s[1],2), round(heading,1)],
            goal=[round(g[0],2), round(g[1],2)], radius=GOAL_RADIUS,
            path_m=round(d,2), turn=cls, turn_deg=round(rel,1),
            instruction=TURNS[cls][sid % 3]))
        sid += 1
    if sid >= 40: break

json.dump(scenarios, open('/tmp/bench/scenarios.json','w'), indent=1)
from collections import Counter
print(f"{len(scenarios)} scenarios from {len({tuple(s['spawn']) for s in scenarios})} spawn poses")
print(" turn mix:", dict(Counter(s['turn'] for s in scenarios)))
print(" path length: min %.1f  median %.1f  max %.1f m" % (
    min(s['path_m'] for s in scenarios),
    sorted(s['path_m'] for s in scenarios)[len(scenarios)//2],
    max(s['path_m'] for s in scenarios)))
