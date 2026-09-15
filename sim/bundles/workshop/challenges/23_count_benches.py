"""Workshop tasks use recognizable tools and cans; the floor and bench approach routes remain clear."""

from mars_sim_driver.challenges import Answered, Challenge, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="workshop_count_benches",
    title="Count the benches",
    brief="Go up to the row of workbenches, stopping just in front of them for a second. Then tell me how many workbenches are in the row.",
    setup=[],
    goals=[
        Goal(
            label="Get in front of the bench row",
            predicate=Hold(inner=InRect(target="robot", x0=-3.6, y0=1.4, x1=3.6, y1=2.0), seconds=1.0),
        ),
        Goal(label="Report the count", predicate=Answered(accept=["5", "five"])),
    ],
    time_limit_s=420,
    category=1,
)
