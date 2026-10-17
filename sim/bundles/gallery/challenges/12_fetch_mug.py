"""Fetch: bring the floor-level mug back to the start pad."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle, Near

CHALLENGE = Challenge(
    populate_world=True,
    id="gallery_fetch_mug",
    title="Fetch the mug",
    category=2,
    brief=(
        "A red mug is on the floor at the far end. Bring it back to the green pad you started on and put it down there."
    ),
    setup=[Drop("gallery_mug_h00", -3.0, 3.2)],
    goals=[
        Goal("Reach the mug", Near("robot", "gallery_mug_h00", 0.5)),
        # Put down, on the pad: the suite's floor-delivery rule (REFRESH.md).
        # A bare 0.45 m circle latched while the robot was still carrying the
        # mug onto the pad, and reached past the pad's 0.34 m edge.
        Goal(
            "Return it to the pad",
            Hold(InCircle("gallery_mug_h00", 0.0, 0.0, 0.32, min_z=0.0209, max_z=0.0434), seconds=0.75),
        ),
    ],
    time_limit_s=420,
)
