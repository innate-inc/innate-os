"""Rounds uses visible coloured door frames, room contents and a floor delivery mat."""

from mars_sim_driver.challenges import Challenge, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="rounds_find_bathroom",
    title="Find the bathroom",
    brief="One of the rooms off this corridor is a bathroom, with a sink and a toilet. Go and find it. Stop inside for two seconds.",
    setup=[],
    goals=[
        Goal(
            label="In the bathroom",
            predicate=Hold(inner=InRect(target="robot", x0=3.2, y0=-3.6, x1=5.8, y1=-1.0), seconds=1.5),
        )
    ],
    time_limit_s=420,
    category=1,
)
