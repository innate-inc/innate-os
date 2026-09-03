"""Rewrite the VLN route descriptions from the geometry of the route.

The first version measured a turn between consecutive 0.9 m samples, so a
corridor that bends gradually -- 20 to 35 degrees a sample -- was reported as
two or three turns, and truncating the list to the first two could name a left
that the route follows with a right. A description has to come from the shape
of the path, not its sampling rate.

So the route is simplified to straight legs first (Douglas-Peucker), and a turn
is the angle where two legs meet. Gradual bends collapse into the one turn a
person would describe; the terminus is kept from reword.py.
"""
import json, math, sys
import cv2, numpy as np

RES, OX, OY = 0.05, -11.0884, -7.4106
SIMPLIFY_M = 0.5      # a wobble smaller than this is not a leg
TURN_DEG = 50.0       # below this the route bends; above it, a person turns

m = cv2.imread(sys.argv[1], cv2.IMREAD_GRAYSCALE)
H, W = m.shape
dist = cv2.distanceTransform((m > 250).astype(np.uint8), cv2.DIST_L2, 5) * RES
n, lab = cv2.connectedComponents((dist > 0.22).astype(np.uint8), 8)
reach = lab == max(((lab == i).sum(), i) for i in range(1, n))[1]
K8 = np.ones((3, 3), np.uint8)

def cell(x, y): return int(round(H - (y - OY)/RES)), int(round((x - OX)/RES))
def world(r, c): return c*RES + OX, (H - r)*RES + OY

def bfs(goal):
    d = np.full((H, W), np.inf, np.float32); f = np.zeros((H, W), np.uint8)
    f[cell(*goal)] = 1; d[cell(*goal)] = 0.0; step = 0
    while f.any():
        step += 1
        grown = cv2.dilate(f, K8).astype(bool) & reach & np.isinf(d)
        d[grown] = step * RES; f = grown.astype(np.uint8)
    return d

def route(a, b, every=0.3):
    d = bfs(b); r, c = cell(*a); pts = [world(r, c)]; trav = 0.0
    for _ in range(4000):
        best = None
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                nr, nc = r+dr, c+dc
                if (dr or dc) and 0 <= nr < H and 0 <= nc < W and reach[nr, nc]:
                    if best is None or d[nr, nc] < d[best[0], best[1]]: best = (nr, nc)
        if best is None or d[best[0], best[1]] >= d[r, c]: break
        trav += RES * (1.414 if best[0] != r and best[1] != c else 1.0)
        r, c = best
        if trav >= every: pts.append(world(r, c)); trav = 0.0
    pts.append(tuple(b))
    return pts

def simplify(pts, eps):
    """Douglas-Peucker: keep the corners, drop the sampling."""
    if len(pts) < 3: return list(pts)
    a, b = np.array(pts[0]), np.array(pts[-1])
    seg = b - a
    L = np.linalg.norm(seg)
    if L < 1e-9:
        d = [np.linalg.norm(np.array(p) - a) for p in pts]
    else:
        d = [abs(np.cross(seg, np.array(p) - a)) / L for p in pts]
    i = int(np.argmax(d))
    if d[i] <= eps: return [pts[0], pts[-1]]
    return simplify(pts[:i+1], eps)[:-1] + simplify(pts[i:], eps)

def wrap(x): return (x + 180) % 360 - 180
def brg(p, q): return math.degrees(math.atan2(q[1]-p[1], q[0]-p[0]))

def legs_of(spawn, goal):
    pts = simplify(route((spawn[0], spawn[1]), goal), SIMPLIFY_M)
    turns, cur = [], spawn[2]
    for i in range(len(pts) - 1):
        b = brg(pts[i], pts[i+1])
        t = wrap(b - cur)
        if abs(t) >= TURN_DEG:
            # consecutive same-way turns are one corner seen twice
            if turns and (t > 0) == (turns[-1] > 0) and abs(wrap(b - cur)) < 100:
                turns[-1] += t
            else:
                turns.append(t)
        cur = b
    return turns

doc = json.load(open(sys.argv[2]))
changed = 0
for sc in doc["scenarios"]:
    if sc.get("suite") != "families" or sc["family"] != "r2r": continue
    tail = sc["instruction"].split(", then ", 1)[-1]
    turns = legs_of(sc["spawn"], tuple(sc["goals"][0]))
    steps = ["Go forward"] + ["turn left" if t > 0 else "turn right" for t in turns[:2]]
    new = ", ".join(steps) + ", then " + tail
    if new != sc["instruction"]:
        print(f"  {sc['id']}: {sc['instruction']}\n      -> {new}")
        sc["instruction"] = new
        changed += 1
json.dump(doc, open(sys.argv[2], "w"), indent=1)
print(f"\nrewrote {changed} VLN descriptions")
