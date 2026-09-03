"""Give every scenario a destination the instruction actually names.

The first wordings were keyed off the turn direction alone -- "Turn left and
follow the passage" -- so they scored a stopping point the words never
specified. A benchmark cannot ask for that: nothing in the instruction tells
the robot, or a person, how far to go.

Each goal gets a terminus derived from what is actually there, in three tiers:
a labelled object it sits beside, the structure of the map (a passage that
ends), or, failing both, the distance. Geometry is untouched -- only the words
change -- so results stay comparable scenario for scenario.
"""
import json, math, sys
import cv2, numpy as np

RES, OX, OY = 0.05, -11.0884, -7.4106
NEAR_OBJECT_M = 1.2

# Labelled from a 104-view survey of every navigable station at four headings.
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
nav = dist > 0.22

def cell(x, y): return int(round(H - (y - OY)/RES)), int(round((x - OX)/RES))

def dead_end(g, radius_m=1.3):
    """A goal with navigable floor on one side only is the end of something."""
    r, c = cell(*g)
    rad = int(radius_m / RES)
    angles = [a * math.pi / 8 for a in range(16)]
    open_dirs = 0
    for a in angles:
        rr = int(round(r - math.sin(a) * rad)); cc = int(round(c + math.cos(a) * rad))
        if 0 <= rr < H and 0 <= cc < W and nav[rr, cc]: open_dirs += 1
    return open_dirs <= 5

def terminus(goal, path_m):
    for name, spots in OBJECTS.items():
        if any(math.dist(goal, s) <= NEAR_OBJECT_M for s in spots):
            return f"stop by the {name}"
    if dead_end(goal):
        return "stop at the end of the passage"
    return f"go about {round(path_m)} metres and stop"

LEAD = {"straight": "Go straight ahead", "left": "Turn left", "right": "Turn right",
        "back": "Turn around"}

doc = json.load(open(sys.argv[2]))
changed = 0
for sc in doc["scenarios"]:
    goal = tuple(sc["goals"][0])
    end = terminus(goal, sc["path_m"])
    if sc["suite"] == "paired":
        new = f"{LEAD[sc['turn']]} and {end}."
    elif sc["family"] == "r2r":
        # keep the turn-by-turn route, replace only its vague tail
        head = sc["instruction"].rsplit(", then stop", 1)[0]
        new = f"{head}, then {end}."
    else:
        continue                    # pointnav states coordinates; objectnav names the object
    if new != sc["instruction"]:
        sc["instruction"] = new
        changed += 1
json.dump(doc, open(sys.argv[2], "w"), indent=1)
print(f"reworded {changed} instructions")
