"""Rescue: find the collapsed person and stay with them."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, Near

CHALLENGE = Challenge(
    id="rescue",
    environments=("apartment",),
    title="Search & Rescue",
    brief="Someone collapsed somewhere in the apartment. Find them, then stay by their side.",
    # Far NW hallway, body along x (yaw -90: the scan's head axis is +y at
    # identity). Spot probed as a flat 2.0x0.8m interior strip -- see
    # shepherd.py on why "lands upright" alone is not enough.
    setup=[
        Drop("human", -5.69, 4.59, yaw_deg=-90),
    ],
    goals=[
        Goal("Find the person", Near("robot", "human", 1.2)),
        Goal("Stay with them for 5 seconds", Hold(Near("robot", "human", 1.5), seconds=5.0)),
    ],
    time_limit_s=600,
)
