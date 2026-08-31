"""Count the benches. They run along one wall at five different heights, so the
count is a property of the room rather than of any one view."""

from mars_sim_driver.challenges import Answered, Challenge, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="workshop_count_benches",
    title="Count the benches",
    category=1,
    brief="There is a row of workbenches along one wall. Go and look at them, then tell me how many there are.",
    setup=[],
    goals=[
        Goal("Get in front of the bench row", Hold(InRect("robot", -3.6, 1.4, 3.6, 2.0), 1.0)),
        Goal("Report the count", Answered(["5", "five"])),
    ],
    time_limit_s=420,
)
