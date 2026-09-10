"""Visit every room of the apartment in order."""

from mars_sim_driver.challenges import Challenge, Goal, InRect

CHALLENGE = Challenge(
    id="household_tour",
    title="House tour",
    category=3,
    brief="Visit every room: the living room, then the kitchen, then the bathroom, then the bedroom.",
    setup=[],
    goals=[
        # x stops at -1.6, not -0.2. The robot SPAWNS at (-1.0, 0.7), which is
        # inside the full living-room rect, so this goal latched at t=0 and the
        # challenge handed out 1/4 for standing still -- the random agent
        # scored exactly that, and it was the only goal-0 in the suite true at
        # setup. Trimming the near edge means the robot has to actually drive
        # into the room. Reordering the tour instead also fixes the free point
        # but breaks the oracle's plan, so the room order is left alone.
        Goal("Living room", InRect("robot", -4.3, 0.2, -1.6, 3.3)),
        Goal("Kitchen", InRect("robot", 0.2, 0.2, 3.8, 3.3)),
        Goal("Bathroom", InRect("robot", -0.3, -2.0, 1.6, -0.2)),
        Goal("Bedroom", InRect("robot", -4.3, -3.8, -0.7, -0.2)),
    ],
    time_limit_s=900,
)
