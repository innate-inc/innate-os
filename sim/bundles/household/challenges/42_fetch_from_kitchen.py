"""Cross-room fetch: kitchen mug to a person in the living room.

THE BRIEF NAMED SOMEONE THE SCENE NEVER SPAWNED. It said "bring it to the
person in the living room" while the setup dropped only the mug, and the goal
was a bare circle around a marker pad. A robot could search the living room
correctly, find no person, and fail; the oracle passed only because it is
handed the destination coordinates. Found in sim/bench/ENVIRONMENT_AUDIT.md,
which is also explicit that widening the hidden circle would not have supplied
the missing information.

Casey now stands in the living room and the delivery is judged against Casey,
so what the brief names, what the camera sees and what the judge measures are
the same thing. test_household_scene.py pins that agreement: if the recipient
moves and this goal does not, the test fails rather than the robot.
"""

from mars_sim_driver.challenges import AllOf, Challenge, Drop, Goal, Hold, InCircle, Near

# Casey stands behind the living-room mat, facing into the room. Measured over
# the 1.5 s after a reset: drift 0.000 m, so the goal cannot drift away from
# the person it names.
RECIPIENT = (-1.3, 2.95)
# The drop spot, 0.75 m in front of Casey. NOT the mat's own centre at
# (-1.3, 2.6): a 0.34 m marker post stands there, and with the post inflated
# on the nav grid there is no cell a robot can occupy while releasing onto it.
# Measured: a route from the spawn stops at (-1.33, 2.17), inside 0.1 m of
# this point, and at no other candidate around the mat.
DROP_SPOT = (-1.3, 2.2)
# Mug rest_z is 0.0279. Same margin the refreshed counter deliveries use,
# rest_z - 4 mm to rest_z + 15 mm, so an airborne or buried mug is not a
# delivery.
MUG_FLOOR = (0.0239, 0.0429)

CHALLENGE = Challenge(
    id="household_fetch_mug",
    title="Mug from the kitchen",
    category=2,
    brief=(
        "There is a blue mug on the kitchen floor. Casey is waiting in the living room. "
        "Bring the mug over and put it down on the round cream mat in front of them."
    ),
    # ON THE FLOOR at 2.75, not on the counter at 2.85. The counter top is
    # 908 mm up; the arm works below ~300 mm and the base cannot climb, so the
    # mug was unpickable at any position -- a fetch nothing could complete.
    # The earlier fix corrected the HORIZONTAL reach and never checked the
    # height, which is exactly the gap a check of the resting height closes.
    #
    # Previous note, still true horizontally: at 3.10 the mug was 0.51 m from
    # the nearest cell the robot can occupy, because free space beside the
    # counter stops at y=2.59 (its footprint plus the 0.145 m planner
    # inflation) and the arm reaches ~0.30 m past the base centre.
    setup=[
        Drop(name="household_mug_kitchen", x=1.4, y=2.75, z=0.033),
        # yaw 0 faces -y, into the room and toward the mat. 180 had Casey
        # facing the wall, so the mat "in front of them" was behind them.
        Drop(name="resident_casey", x=RECIPIENT[0], y=RECIPIENT[1], yaw_deg=0),
    ],
    goals=[
        Goal(label="Reach the mug", predicate=Near(a="robot", b="household_mug_kitchen", radius_m=0.55)),
        # Two claims, both required: the mug is beside the person the brief
        # named, and it is resting on the floor rather than held, buried or in
        # the air. 0.75 s of dwell is the refreshed suite's settling interval.
        Goal(
            label="Put it down by Casey",
            predicate=Hold(
                inner=AllOf(
                    preds=[
                        InCircle(
                            target="household_mug_kitchen",
                            x=DROP_SPOT[0],
                            y=DROP_SPOT[1],
                            radius_m=0.45,
                            min_z=MUG_FLOOR[0],
                            max_z=MUG_FLOOR[1],
                        ),
                        # 1.0 m, not 0.7: the circle above already fixes where
                        # the mug goes. This one only holds the goal and the
                        # recipient together, so "beside Casey" cannot quietly
                        # become a coordinate with nobody at it.
                        Near(a="household_mug_kitchen", b="resident_casey", radius_m=1.0),
                    ]
                ),
                seconds=0.75,
            ),
        ),
    ],
    time_limit_s=900,
)
