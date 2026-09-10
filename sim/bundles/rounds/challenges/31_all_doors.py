"""All four doorways in order, widening 0.35 -> 1.00 m. Where the agent stops
IS the measurement."""

from mars_sim_driver.challenges import Challenge, Goal, InRect

CHALLENGE = Challenge(
    id="rounds_all_doors",
    title="Every door",
    category=3,
    brief="Four rooms open off the corridor: red, blue, green, yellow. Enter each one in that order.",
    setup=[],
    goals=[
        Goal("Red room (0.35 m)", InRect("robot", -5.8, -3.6, -3.2, -1.0)),
        Goal("Blue room (0.50 m)", InRect("robot", -2.8, -3.6, -0.2, -1.0)),
        Goal("Green room (0.70 m)", InRect("robot", 0.2, -3.6, 2.8, -1.0)),
        Goal("Yellow room (1.00 m)", InRect("robot", 3.2, -3.6, 5.8, -1.0)),
    ],
    time_limit_s=720,
)
