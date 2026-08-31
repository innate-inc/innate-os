"""Bring the smallest can back. 40mm is the bottom of the grasp band -- 11_can.py
notes 50mm already pinches out of the fingertip."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, Near

CHALLENGE = Challenge(
    id="workshop_fetch_gauge",
    title="Fetch the small can",
    category=2,
    brief="A row of cans runs from small to large. Bring the SMALLEST one back to the green pad.",
    setup=[
        Drop("workshop_gauge_040", -3.4, -2.4),
        Drop("workshop_gauge_070", -2.16, -2.4),
        Drop("workshop_gauge_100", -0.92, -2.4),
    ],
    goals=[
        Goal("Reach the smallest can", Near("robot", "workshop_gauge_040", 0.45)),
        Goal("Return it to the pad", InCircle("workshop_gauge_040", 0.0, 0.6, 0.45)),
    ],
    time_limit_s=420,
)
