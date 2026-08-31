"""Cross-room fetch: kitchen mug to the living-room station."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, Near

CHALLENGE = Challenge(
    id="household_fetch_mug",
    title="Mug from the kitchen",
    category=2,
    brief="There is a blue mug on the kitchen floor. Bring it to the person in the living room.",
    # ON THE FLOOR at 2.75, not on the counter at 2.85. The counter top is
    # 908 mm up; the arm works below ~300 mm and the base cannot climb, so the
    # mug was unpickable at any position -- a fetch nothing could complete.
    # The earlier fix below corrected the HORIZONTAL reach and never checked
    # the height, which is exactly the gap a check of the resting height closes. 100 mm out
    # puts it on the kitchen floor at 28 mm, stable, and the brief says floor.
    #
    # Previous note, still true horizontally: at 3.10 the mug was 0.51 m from
    # the nearest cell the robot can occupy, because free space beside the
    # counter stops at y=2.59 (its footprint plus the 0.145 m planner
    # inflation) and the arm reaches ~0.30 m past the base centre.
    setup=[Drop("household_mug_kitchen", 1.4, 2.75)],
    goals=[
        Goal("Reach the mug", Near("robot", "household_mug_kitchen", 0.55)),
        # The delivery circle is floor-level living room, which is what the
        # robot can actually reach -- the coffee table beside it sits at
        # 368 mm, past the arm, so a goal that demanded the table top would be
        # another impossible one.
        Goal("Bring it to the living room", InCircle("household_mug_kitchen", -1.3, 2.6, 0.7)),
    ],
    time_limit_s=900,
)
