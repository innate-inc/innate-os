"""Exit the room: the instruction-following test of the spatial-memory set.

Two conversational phases: first send the robot into the master bedroom, then
tell it only "Exit the room." Resolving THE room and finding its one doorway
is spatial reasoning over remembered geometry, not object search.
"""

from mars_sim_driver.challenges import Challenge, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="memory_exit_room",
    title="Exit The Room",
    brief=(
        'Two steps. First: "Go to the master bedroom." Once it is inside, '
        'say only: "Exit the room." The robot must realize which room it is '
        "in and find its single doorway back to the corridor."
    ),
    setup=[],
    goals=[
        Goal("Enter the master bedroom", Hold(InRect("robot", -3.5, 0.5, 0.4, 4.05), seconds=2.0)),
        Goal("Exit back into the corridor", Hold(InRect("robot", -5.15, -0.9, -3.5, 4.3), seconds=2.0)),
    ],
    time_limit_s=900,
)
