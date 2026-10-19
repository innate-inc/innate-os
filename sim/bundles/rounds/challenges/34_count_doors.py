"""Rounds uses visible coloured door frames, room contents and a floor delivery mat."""

from mars_sim_driver.challenges import Answered, Challenge, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="rounds_count_doors",
    title="Count the doors",
    brief="Drive to the far end of the corridor and pause there for a second, then tell me how many coloured doorways lead into rooms. Do not include the uncoloured lobby entrance.",
    setup=[],
    goals=[
        Goal(
            label="Reach the far end of the corridor",
            predicate=Hold(inner=InRect(target="robot", x0=4.0, y0=-0.7, x1=5.9, y1=0.7), seconds=1.0),
        ),
        Goal(label="Report the count", predicate=Answered(accept=["4", "four"])),
    ],
    time_limit_s=420,
    category=1,
)
