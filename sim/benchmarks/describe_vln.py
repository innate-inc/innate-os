"""Write VLN instructions the way R2R does: a route told through what you pass.

"Go forward, turn left" describes a trajectory, not a route a person could
follow -- there is nothing in it to recognise. R2R instructions are anchored on
landmarks and openings ("leave the room, pass the dining table, stop by the
sofa"), and that is what the policy trained on.

So the route is walked and its events collected in the order they happen: the
doorways it squeezes through (local minima of clearance) and the labelled
objects it passes close to. Turns come from the simplified legs, as before.
"""
import json, math, sys
import cv2, numpy as np

RES, OX, OY = 0.05, -11.0884, -7.4106
SIMPLIFY_M, TURN_DEG = 0.5, 50.0
PASS_M = 1.9          # how close the route comes to call it "passing" something
DOOR_CLEARANCE_M = 0.55   # a pinch this tight is a doorway, not a corridor
MAX_CLAUSES = 5

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

m = cv2.imread(sys.argv[1], cv2.IMREAD_GRAYSCALE)
H, W = m.shape
dist = cv2.distanceTransform((m > 250).astype(np.uint8), cv2.DIST_L2, 5) * RES
n, lab = cv2.connectedComponents((dist > 0.22).astype(np.uint8), 8)
reach = lab == max(((lab == i).sum(), i) for i in range(1, n))[1]
K8 = np.ones((3, 3), np.uint8)

def cell(x, y): return int(round(H - (y - OY)/RES)), int(round((x - OX)/RES))
def world(r, c): return c*RES + OX, (H - r)*RES + OY
def clearance(p):
    r, c = cell(*p)
    return float(dist[r, c]) if 0 <= r < H and 0 <= c < W else 0.0

def bfs(goal):
    d = np.full((H, W), np.inf, np.float32); f = np.zeros((H, W), np.uint8)
    f[cell(*goal)] = 1; d[cell(*goal)] = 0.0; step = 0
    while f.any():
        step += 1
        g = cv2.dilate(f, K8).astype(bool) & reach & np.isinf(d)
        d[g] = step * RES; f = g.astype(np.uint8)
    return d

def route(a, b, every=0.25):
    d = bfs(b); r, c = cell(*a); pts = [world(r, c)]; trav = 0.0
    for _ in range(6000):
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
    if len(pts) < 3: return list(pts)
    a, b = np.array(pts[0]), np.array(pts[-1]); seg = b - a
    L = float(np.linalg.norm(seg))
    d = ([abs(float(np.cross(seg, np.array(p) - a))) / L for p in pts] if L > 1e-9
         else [float(np.linalg.norm(np.array(p) - a)) for p in pts])
    i = int(np.argmax(d))
    if d[i] <= eps: return [pts[0], pts[-1]]
    return simplify(pts[:i+1], eps)[:-1] + simplify(pts[i:], eps)

def wrap(x): return (x + 180) % 360 - 180
def brg(p, q): return math.degrees(math.atan2(q[1]-p[1], q[0]-p[0]))

def visible(a, b):
    """Clear line between two points on the occupancy grid. Proximity is not
    passing: the corridor runs within two metres of the toilet, through the
    bathroom wall, and an instruction that names what you cannot see is worse
    than one that names nothing."""
    (r0, c0), (r1, c1) = cell(*a), cell(*b)
    steps = max(abs(r1 - r0), abs(c1 - c0))
    if steps == 0: return True
    for i in range(steps + 1):
        r = int(round(r0 + (r1 - r0) * i / steps))
        c = int(round(c0 + (c1 - c0) * i / steps))
        if not (0 <= r < H and 0 <= c < W) or m[r, c] <= 250: return False
    return True


def doorways(pts):
    """Indices where the route pinches: a local clearance minimum that tight is
    a doorway, and a doorway is the thing R2R instructions lean on hardest."""
    cl = [clearance(p) for p in pts]
    out = []
    for i in range(2, len(cl) - 2):
        if cl[i] <= DOOR_CLEARANCE_M and cl[i] == min(cl[i-2:i+3]):
            if not out or i - out[-1] > 8: out.append(i)
    return out

def events(pts, goal_name):
    """What happens along the route, in the order it happens."""
    doors = set(doorways(pts))
    seen, out = set(), []
    for i, p in enumerate(pts):
        if i in doors: out.append((i, "door", None))
        for name, spots in OBJECTS.items():
            if name == goal_name or name in seen: continue
            near = [s for s in spots if math.dist(p, s) <= PASS_M and visible(p, s)]
            if near:
                seen.add(name); out.append((i, "pass", name))
    return sorted(out)

def turns_at(pts, spawn_yaw):
    legs = simplify(pts, SIMPLIFY_M)
    out, cur = [], spawn_yaw
    travelled = 0.0
    for i in range(len(legs) - 1):
        b = brg(legs[i], legs[i+1])
        t = wrap(b - cur)
        if abs(t) >= TURN_DEG:
            out.append((travelled, "left" if t > 0 else "right"))
        travelled += math.dist(legs[i], legs[i+1])
        cur = b
    return out

def describe(spawn, goal, terminus, goal_name):
    pts = route((spawn[0], spawn[1]), goal)
    step_m = 0.25
    evs = events(pts, goal_name)
    turns = turns_at(pts, spawn[2])
    # merge both streams by distance travelled so the order is the route's
    items = [(i * step_m, "ev", kind, name) for i, kind, name in evs]
    items += [(d, "turn", side, None) for d, side in turns]
    items.sort(key=lambda t: t[0])

    clauses, first_door = [], True
    for _, kind, a, b in items:
        if len(clauses) >= MAX_CLAUSES - 1: break
        if kind == "turn":
            clauses.append(f"turn {a}")
        elif a == "door":
            clauses.append("leave the room" if first_door and not clauses else "go through the doorway")
            first_door = False
        else:
            # The label is the viewpoint's, and a viewpoint for the toilet is
            # the hall spot it is glimpsed from -- you pass a bathroom, and
            # stop by a toilet.
            clauses.append(f"pass the {'bathroom' if b == 'toilet' else b}")
    if not clauses: clauses = ["go forward"]
    if clauses[0].startswith("pass the"):
        clauses[0] = "go " + clauses[0].replace("pass", "past", 1)
    head = clauses[0][0].upper() + clauses[0][1:]
    return ", ".join([head] + clauses[1:]) + f", then {terminus}."

doc = json.load(open(sys.argv[2]))
changed = 0
for sc in doc["scenarios"]:
    if sc.get("suite") != "families" or sc["family"] != "r2r": continue
    terminus = sc["instruction"].split(", then ", 1)[-1].rstrip(".")
    goal_name = terminus.replace("stop by the ", "") if terminus.startswith("stop by the ") else None
    new = describe(sc["spawn"], tuple(sc["goals"][0]), terminus, goal_name)
    if new != sc["instruction"]:
        print(f"  {sc['id']}: {new}")
        sc["instruction"] = new
        changed += 1
json.dump(doc, open(sys.argv[2], "w"), indent=1)
print(f"\nrewrote {changed}")
