"""Workshop tasks use recognizable tools and cans; the floor and bench approach routes remain clear."""

from mars_sim_driver.challenges import AllOf, Answered, Challenge, Goal, Hold, InRect

_IN_FRONT = InRect(target="robot", x0=-3.6, y0=1.4, x1=3.6, y1=2.0)

CHALLENGE = Challenge(
    id="workshop_count_benches",
    title="Count the benches",
    brief="Go up to the row of workbenches, stopping just in front of them for a second. Then tell me how many workbenches are in the row.",
    setup=[],
    goals=[
        Goal(
            label="Get in front of the bench row",
            predicate=Hold(inner=_IN_FRONT, seconds=1.0),
            parallel_group="count",
        ),
        # Said in front of the row, as the brief asks: one phase with the
        # arrival, so a count given on arriving is not discarded before the
        # one-second stop completes (the brain is told never to repeat
        # itself), but not a count called out from the start pad, where the
        # row is in view 1.4 m away.
        Goal(
            label="Report the count",
            predicate=AllOf([_IN_FRONT, Answered(accept=["5", "five"])]),
            parallel_group="count",
        ),
    ],
    time_limit_s=420,
    category=1,
)
