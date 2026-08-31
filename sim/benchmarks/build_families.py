"""Three benchmark families, each with ground truth the sim can verify.

pointnav   the goal IS the instruction (ego-frame coordinates), so ground
           truth is exact and a failure cannot be blamed on wording.
objectnav  ground truth is a viewpoint: a station the object is prominently
           visible from and close to. Several instances count -- "find the
           bed" is satisfied by either bedroom -- so a scenario carries every
           acceptable viewpoint and is scored against the nearest.
vln        a turn-by-turn route read off the geodesic path, ending at a named
           landmark where there is one. Ground truth is the route's end.
"""
import json, math
import cv2, numpy as np

RES, OX, OY = 0.05, -11.0884, -7.4106
GOAL_RADIUS, MIN_PATH, MAX_PATH = 0.75, 1.5, 7.0
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
def geo(a, b):
    r, c = cell(*b); return float(dmap[a][r, c])

def route(a, b, step=0.9):
    """Sample the actual route a -> b every ~step metres."""
    d = dmap[b]; r, c = cell(*a); pts = [world(r, c)]; trav = 0.0
    while True:
        best = None
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                nr, nc = r+dr, c+dc
                if (dr or dc) and 0 <= nr < H and 0 <= nc < W and reach[nr, nc]:
                    if best is None or d[nr, nc] < d[best[0], best[1]]: best = (nr, nc)
        if best is None or d[best[0], best[1]] >= d[r, c]: break
        trav += RES * (1.414 if best[0] != r and best[1] != c else 1.0)
        r, c = best
        if trav >= step: pts.append(world(r, c)); trav = 0.0
    pts.append(b)
    return pts

def wrap(a): return (a + 180) % 360 - 180
def bearing(p, q): return math.degrees(math.atan2(q[1]-p[1], q[0]-p[0]))

# Objects, labelled from a 104-view survey of every station (4 headings each).
# Each entry is the stations the object is prominent and close from.
OBJECTS = {
    "bed":            [(-2.2, 5.9), (-2.3, 4.9), (-0.9, 2.4), (-3.4, 2.2), (-0.8, 1.4), (-3.4, 1.2)],
    "sofa":           [(-4.7, -0.8), (-3.7, -1.1), (-4.2, 0.1)],
    "armchair":       [(-4.2, 0.1), (-4.7, -0.8), (-3.7, -1.1)],
    "dining table":   [(-1.9, -0.7), (-0.8, -0.7), (-1.0, -1.7), (-1.6, -2.5), (-0.5, -2.6)],
    "refrigerator":   [(-1.9, -0.7), (-1.0, -1.7)],
    "oven":           [(-1.9, -0.7), (-1.6, -2.5)],
    "toilet":         [(-4.7, 1.0)],
    "kitchen counter":[(-0.8, -0.7), (-1.0, -1.7), (-0.5, -2.6)],
}
def nearest_station(p):
    return min(cand, key=lambda c: (c[0]-p[0])**2 + (c[1]-p[1])**2)
OBJECTS = {k: [nearest_station(p) for p in v] for k, v in OBJECTS.items()}

pairs = [(a, b, geo(a, b)) for a in cand for b in cand
         if a != b and MIN_PATH <= geo(a, b) <= MAX_PATH]
pairs.sort(key=lambda t: -t[2])

def spread(items, n, key, cap=2):
    """Take n items keeping the spawns varied rather than all from one corner."""
    out, seen = [], {}
    for it in items:
        k = key(it)
        if seen.get(k, 0) >= cap: continue
        seen[k] = seen.get(k, 0) + 1
        out.append(it)
        if len(out) == n: break
    return out

scen = []

# --- pointnav: the goal is the instruction --------------------------------
for i, (a, b, d) in enumerate(spread(pairs, 20, key=lambda t: t[0])):
    heading = bearing(a, route(a, b)[1])
    rel = math.radians(wrap(bearing(a, b) - heading))
    dd = math.dist(a, b)
    fwd, left = dd * math.cos(rel), dd * math.sin(rel)
    brg = math.degrees(math.atan2(left, fwd))
    where = "straight ahead" if abs(brg) < 3 else f"{abs(brg):.0f} degrees to your {'left' if brg > 0 else 'right'}"
    scen.append(dict(
        id=f"pn{i:02d}", family="pointnav", spawn=[round(a[0],2), round(a[1],2), round(heading,1)],
        goals=[[round(b[0],2), round(b[1],2)]], radius=GOAL_RADIUS, path_m=round(d,2),
        instruction=(f"Go to ({fwd:.1f}, {left:.1f}) in your egocentric frame: "
                     f"{math.hypot(fwd,left):.1f} m away, {where}.")))

# --- objectnav: any instance counts ---------------------------------------
onav = []
for cat, views in OBJECTS.items():
    far = [(a, min(geo(a, v) for v in views)) for a in cand
           if 2.5 <= min(geo(a, v) for v in views) <= 8.0 and a not in views]
    far.sort(key=lambda t: -t[1])
    for a, d in far[:4]:
        onav.append((cat, a, d, views))
onav.sort(key=lambda t: -t[2])
for i, (cat, a, d, views) in enumerate(spread(onav, 20, key=lambda t: t[0], cap=3)):
    heading = bearing(a, route(a, min(views, key=lambda v: geo(a, v)))[1])
    scen.append(dict(
        id=f"on{i:02d}", family="objectnav", category=cat,
        spawn=[round(a[0],2), round(a[1],2), round(heading,1)],
        goals=[[round(v[0],2), round(v[1],2)] for v in views],
        radius=GOAL_RADIUS, path_m=round(d,2),
        instruction=f"Find the {cat}."))

# --- vln: turn-by-turn, ending on a landmark where there is one -----------
LANDMARK = {}
for cat, views in OBJECTS.items():
    for v in views: LANDMARK.setdefault(v, cat)

def describe(a, b):
    pts = route(a, b)
    heading = bearing(a, pts[1])
    legs, cur = [], heading
    for i in range(1, len(pts) - 1):
        brg = bearing(pts[i], pts[i+1])
        turn = wrap(brg - cur)
        if abs(turn) > 45:
            legs.append("turn left" if turn > 0 else "turn right")
            cur = brg
    steps = ["Go forward"]
    for t in legs[:2]: steps.append(t)
    tail = f", then stop by the {LANDMARK[b]}" if b in LANDMARK else ", then stop"
    return heading, (", ".join(steps) + tail + ".").capitalize()

vln = [(a, b, d) for a, b, d in pairs if d >= 2.5]
for i, (a, b, d) in enumerate(spread(vln, 20, key=lambda t: t[0])):
    heading, text = describe(a, b)
    scen.append(dict(
        id=f"vl{i:02d}", family="r2r", spawn=[round(a[0],2), round(a[1],2), round(heading,1)],
        goals=[[round(b[0],2), round(b[1],2)]], radius=GOAL_RADIUS, path_m=round(d,2),
        instruction=text))

json.dump(scen, open('/tmp/bench/families.json', 'w'), indent=1)
from collections import Counter
print("built:", Counter(s["family"] for s in scen))
for fam in ("pointnav", "objectnav", "r2r"):
    ex = [s for s in scen if s["family"] == fam][:3]
    print(f"\n{fam}:")
    for s in ex:
        print(f"   {s['id']} path {s['path_m']}m  goals {len(s['goals'])}  {s['instruction']}")
