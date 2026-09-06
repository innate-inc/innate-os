"""Find the marked exit in the Backrooms."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="way_out",
    title="Find a way out",
    brief="You’re in the Backrooms. Guide MARS to the green exit at the end of the corridor.",
    environments=("backrooms",),
    setup=[Drop("exit_marker", 4.5, -5.2)],
    goals=[Goal("Reach the green exit", Hold(InCircle("robot", 3.7, -5.2, 0.7), 1.0))],
    agent_guidance=(
        "You and the user are finding a way out of the Backrooms. The green exit is at the end of the long corridor. "
        "Invite the user to ask you to find the exit. SearchMemory knows a view of the exit and its approach. "
        "Use the recalled map position with NavigateToPosition(local_frame=false). "
        "If navigation fails, explain what happened and offer a shorter next move; wait for the user's retry request."
    ),
)
