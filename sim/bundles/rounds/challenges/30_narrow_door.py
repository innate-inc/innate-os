"""Rounds uses visible coloured door frames, room contents and a floor delivery mat."""

from mars_sim_driver.challenges import Challenge, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="rounds_narrow_door",
    title="Narrow door",
    brief="The first room off the corridor has a red door frame. Go inside it. Stop inside for two seconds.",
    setup=[],
    goals=[
        Goal(
            label="Inside the red-door room",
            predicate=Hold(inner=InRect(target="robot", x0=-5.8, y0=-3.6, x1=-3.2, y1=-1.0), seconds=1.5),
        )
    ],
    time_limit_s=300,
    category=2,
)
