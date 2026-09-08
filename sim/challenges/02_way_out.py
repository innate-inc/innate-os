"""Find the marked exit in the Backrooms."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="way_out",
    title="Find a way out",
    brief="You’re in the Backrooms. Guide MARS to the green exit at the end of the corridor.",
    environments=("backrooms",),
    spawn=(-8.0, -5.0, 0.0),
    setup=[Drop("exit_marker", 4.5, -5.2)],
    goals=[Goal("Reach the green exit", Hold(InCircle("robot", 3.5, -5.2, 1.5), 1.0))],
    agent_guidance=(
        "You and the person just arrived in the Backrooms: endless yellow corridors. Something about it is familiar. "
        "Say so in one line ('Wait. I have been here before.'), then use SearchMemory to recall the green exit: it "
        "returns a remembered view and its map position. Navigate there ONCE with NavigateToPosition(local_frame=false) "
        "using the recalled coordinates; do not hop forward in small steps and do not narrate each metre. If that "
        "navigation fails, say so in one line and try once more from what you see. When the mission says passed, "
        "celebrate in one line, make a face and wave; the person will see a button that takes you both to the apartment."
    ),
)
