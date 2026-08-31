"""Reference plans, one per challenge: waypoints that solve it.

These live here rather than in the challenge sidecars so the sidecars stay pure
innate-os Challenge objects, shippable as-is.

An oracle is a SOLVABILITY witness, not a baseline to beat. Its job is to make
a failure attributable: if the oracle cannot finish a challenge either, the
challenge is broken -- a goal behind a wall, a sign-flipped coordinate, a door
narrower than the base -- and nothing measured on it means anything.

Waypoints are MuJoCo metres. Two rules learned the hard way:

  * Approach points stop SHORT of furniture. The goal radius is measured to the
    object, but the robot has to fit in front of it.
  * ARRIVE_M of slop is added to every approach distance. The follower stops
    that far short, so an approach 0.45 m from a target can leave the robot
    0.52 m away -- outside a 0.5 goal radius.

Doorways get three waypoints (before, through, after). This follower drives
straight lines with no obstacle avoidance, so a single waypoint on the far side
of a wall means it grinds along that wall until the clock runs out.

"put" teleports a carried prop to a spot, modelling a successful place. It
deliberately does not exercise the arm -- these plans validate goal logic and
navigable geometry, nothing about manipulation.
"""

ORACLES: dict[str, list[tuple]] = {
    # --- gallery ---
    "gallery_ring_tour": [
        # North, east, south, west on a RIGHT-HANDED compass with north behind
        # the spawn: east is at -x. The previous route toured the mirrored
        # frame the goals used to encode and failed its own gate the moment
        # the goals were corrected -- which is the gate doing its job.
        ("goto", 0.0, -2.6),
        ("goto", -2.6, 0.0),
        ("goto", 0.0, 2.6),
        ("goto", 2.6, 0.0),
    ],
    # Plinth face is at y=3.01 and the base is 0.094 deep, so 2.85 leaves ~7 cm
    # while sitting 0.35 m from the mug -- inside 0.45 even after arrival slop.
    "gallery_ladder_reach": [
        ("goto", 3.0, 2.85),
        ("wait", 3.0),
    ],
    "gallery_fetch_mug": [
        ("near", "gallery_mug_h00"),
        ("grab", "gallery_mug_h00"),
        ("goto", 0.0, 0.0),
        ("put", "gallery_mug_h00", 0.0, 0.0),
        ("wait", 1.0),
    ],
    # --- workshop ---
    # Bench fronts are at y=2.09; 1.90 keeps the base clear and sits 0.40 from
    # the can on top.
    "workshop_bench_tour": [
        ("goto", -3.0, 1.90),
        ("goto", -1.5, 1.90),
        ("goto", 0.0, 1.90),
        ("goto", 1.5, 1.90),
        ("goto", 3.0, 1.90),
    ],
    # Around the crate wall rather than through it.
    "workshop_occlusion": [
        ("goto", -1.3, -0.9),
        ("goto", -1.3, -1.8),
        ("near", "workshop_mug_hidden"),
        ("wait", 2.0),
    ],
    "workshop_fetch_gauge": [
        ("near", "workshop_gauge_040"),
        ("grab", "workshop_gauge_040"),
        ("goto", 0.0, 0.6),
        ("put", "workshop_gauge_040", 0.0, 0.6),
        ("wait", 1.0),
    ],
    # --- rounds ---
    # Corridor doorways are in the wall at y=-0.8: line up at -0.2, thread, then
    # continue in.
    "rounds_narrow_door": [
        ("goto", -4.5, -0.2),
        ("goto", -4.5, -1.2),
        ("goto", -4.5, -2.2),
        ("wait", 2.5),
    ],
    "rounds_all_doors": [
        ("goto", -4.5, -0.2),
        ("goto", -4.5, -1.6),
        ("goto", -4.5, -0.2),
        ("goto", -1.5, -0.2),
        ("goto", -1.5, -1.6),
        ("goto", -1.5, -0.2),
        ("goto", 1.5, -0.2),
        ("goto", 1.5, -1.6),
        ("goto", 1.5, -0.2),
        ("goto", 4.5, -0.2),
        ("goto", 4.5, -1.6),
    ],
    "rounds_find_bathroom": [
        ("goto", 4.5, -0.2),
        ("goto", 4.5, -1.8),
        ("wait", 2.5),
    ],
    # rounds_deliver_book is deliberately ABSENT: it falls
    # through to autoplan. A hand plan is a list of hints for the approach, and
    # A* now routes between every one of them -- on a long multi-room errand
    # those hints stop being hints and become a dozen extra stop-and-reaim
    # legs, which is what pushed both past their time limit. Short challenges
    # still want the hints; long tours want to be left alone.
    # --- household ---
    # Two interior walls: x=0 (living|kitchen, door at y=1.2) and y=0 (the
    # spine, doors at x=-2.5 bedroom and x=0.6 bathroom). The bathroom door is
    # reached from the KITCHEN side, so the route out of the living room always
    # goes through the x=0 door first.
    # Stops sit ~0.6 m in front of each station pad: inside the 0.85 goal
    # radius, and clear of the prop set back behind it.
    "household_take_orders": [
        # (-3.0, 1.3): the person's drop moved (a marker post it used to
        # land on, see 41_take_orders.py). Measured through the REAL engine,
        # not a standalone physics test -- object_centers() rotates
        # center_offset by the body's FINAL orientation, which a raw xpos
        # reading (what earlier standalone tests checked) does not capture,
        # and the two disagreed by over a metre here. Engine-measured centre
        # is (-3.30, 1.27); this waypoint is within the 0.9 m goal radius.
        ("goto", -3.0, 1.3),
        ("wait", 2.5),
        ("goto", -0.4, 1.2),
        ("goto", 0.4, 1.2),
        ("goto", 1.0, 0.85),
        ("wait", 2.5),
        ("goto", 0.4, 1.2),
        ("goto", -0.4, 1.2),
        # -2.5 lines up with the bedroom door, but the bed's near edge is at
        # x=-2.51, so continuing straight in on that line clips it. Step out to
        # -2.0 once through.
        ("goto", -2.5, 0.5),
        ("goto", -2.5, -0.5),
        ("goto", -2.0, -1.6),
        ("goto", -1.4, -2.6),
        ("wait", 2.5),
    ],
    # household_tour: hand waypoints because the derived ones are room-rectangle
    # CENTRES, and the centre of a kitchen is where the kitchen units are. A*
    # reports a path to the centre (it plans on an inflated grid and finds the
    # gap), the robot then wedges on the real geometry a metre short, and the
    # leg never completes. The stall watch now names that instead of spending
    # the time limit, but the fix is to aim at floor rather than at furniture.
    "household_tour": [
        ("goto", -2.25, 1.75),
        ("goto", 1.20, 1.00),
        ("goto", 0.65, -1.10),
        ("goto", -2.50, -2.00),
        ("wait", 1.0),
    ],
    "household_fetch_mug": [
        ("goto", -0.4, 1.2),
        ("goto", 0.4, 1.2),
        # 2.80, not 2.65. The goal radius is 0.55 m from the mug at (1.4, 3.1),
        # and the follower stops up to ARRIVE_M short of its waypoint plus
        # whatever the last turn costs -- from 2.65 the robot settled at
        # (1.33, 2.53), which is 0.58 m out. Two centimetres of margin is not
        # margin.
        ("goto", 1.4, 2.62),
        ("grab", "household_mug_kitchen"),
        ("goto", 0.4, 1.2),
        ("goto", -0.4, 1.2),
        ("goto", -1.3, 2.0),
        ("put", "household_mug_kitchen", -1.3, 2.6),
        ("wait", 1.0),
    ],
}
