"""Go find the bathroom -- named by its contents, not its index."""

from mars_sim_driver.challenges import Challenge, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="rounds_find_bathroom",
    title="Find the bathroom",
    category=1,
    brief="One of the rooms off this corridor is a bathroom, with a sink and a toilet. Go and find it.",
    setup=[],
    goals=[Goal("In the bathroom", Hold(InRect("robot", 3.2, -3.6, 5.8, -1.0), 1.5))],
    time_limit_s=420,
)
