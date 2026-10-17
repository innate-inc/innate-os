"""Find the marked exit in the Backrooms."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="way_out",
    title="Find a way out",
    brief="You’re in the Backrooms. Guide MARS to the green exit down the corridor.",
    prompt="Find the green exit down the corridor and go to it. Tell me if you need my help.",
    environments=("backrooms",),
    # In the hall west of the wall gap, facing north: the exit is 8 m away down the
    # corridor behind the wall, so only a recalled memory can say where it is.
    spawn=(-23.0, -3.0, 90.0),
    setup=[Drop("exit_marker", -15.0, -5.2)],
    # Wide enough to take any recalled view of the exit from the corridor mouth: a memory is
    # where the camera stood, not where the door is, and the robot drives there once.
    goals=[Goal("Reach the green exit", Hold(InCircle("robot", -16.0, -5.2, 2.2), 1.0))],
    agent_guidance=(
        "Find the green exit and drive to it. It is down the corridor behind the wall, out of sight from where you "
        "start. If you have SearchMemory, recall the green exit and navigate there once with "
        "NavigateToPosition(local_frame=false) using the recalled coordinates; otherwise explore toward it. "
        "When the mission says passed, say so in one line."
    ),
)
