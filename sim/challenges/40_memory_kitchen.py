"""Go to the kitchen: the find-location baseline of the spatial-memory set.

With remembered views of the apartment the robot should search its memory,
get the kitchen's map position, and drive straight there — no wandering.
"""

from mars_sim_driver.challenges import Challenge, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="memory_kitchen",
    title="Kitchen Run",
    brief=(
        'Tell the robot: "Go to the kitchen." A robot that has seen the '
        "apartment before should recall where that is and drive straight "
        "there instead of wandering."
    ),
    setup=[],
    goals=[
        Goal("Reach the kitchen and stay 3s", Hold(InRect("robot", -3.1, -1.1, 0.3, 0.5), seconds=3.0)),
    ],
    time_limit_s=300,
)
