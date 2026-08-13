"""Evening rounds: the long-horizon sequence test of the spatial-memory set.

Three rooms in one instruction, in order, across the whole apartment. Each
leg is easy; holding the plan and knowing where each room is over several
minutes is the test.
"""

from mars_sim_driver.challenges import Challenge, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="memory_rounds",
    title="Evening Rounds",
    brief=(
        'Tell the robot: "Do your evening rounds: check the dining area, '
        'then the office, and finish in the master bedroom." It must visit '
        "all three, in that order."
    ),
    setup=[],
    goals=[
        Goal("Check the dining area", Hold(InRect("robot", -3.1, -4.0, 0.0, -1.1), seconds=2.0)),
        Goal("Check the office", Hold(InRect("robot", -3.5, 4.2, 0.9, 5.35), seconds=2.0)),
        Goal("Finish in the master bedroom", Hold(InRect("robot", -3.5, 0.5, 0.4, 4.05), seconds=2.0)),
    ],
    time_limit_s=1200,
)
