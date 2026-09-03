"""Size each scenario's success region to the precision its instruction gives.

A single radius for everything scores "stop by the bed" as strictly as "go to
(0.9, -5.2)". The bed is two metres long: standing at its foot and standing at
its side are both by the bed, and the words do not choose between them, so a
0.75 m circle marks one of them wrong for no reason a reader could have known.

Tolerance therefore follows the terminus: a coordinate is exact, a named object
is as big as the object plus somewhere to stand, and a passage end or a stated
distance sits between.
"""
import json, sys

RADIUS = {
    "coordinate": 0.75,   # pointnav states the point; hold it to the point
    "object":     1.5,    # furniture-sized, plus room to stand beside it
    "structure":  1.0,    # "the end of the passage" is a place, not a point
    "distance":   1.0,    # "about 7 metres" is stated to the metre
}

def kind(sc):
    if sc["family"] == "pointnav": return "coordinate"
    if sc["family"] == "objectnav": return "object"
    text = sc["instruction"].lower()
    if "stop by the " in text: return "object"
    if "end of the passage" in text: return "structure"
    return "distance"

doc = json.load(open(sys.argv[1]))
counts = {}
for sc in doc["scenarios"]:
    k = kind(sc)
    sc["radius"] = RADIUS[k]
    counts[k] = counts.get(k, 0) + 1
doc["goal_radius_m"] = None          # no single radius any more; each carries its own
doc["note"] = doc["note"].replace(
    "Spawns and goals are generated from the map",
    "Each scenario's success radius matches the precision of its own instruction "
    "-- a coordinate is held to 0.75 m, a named object to 1.5 m, since the words "
    "do not say which side of a bed to stand on. Spawns and goals are generated "
    "from the map")
json.dump(doc, open(sys.argv[1], "w"), indent=2)
print("radii assigned:", counts)
