"""The Nowhere story's last room: the Backrooms, found by memory. The world starts it when the
door is reached; the standalone "Find a way out" is the same room without the story."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="nowhere_way_out",
    title="Find a way out",
    brief="You’re in the Backrooms. Guide MARS to the green exit down the corridor.",
    environments=("backrooms",),
    listed=False,
    # In the hall west of the wall gap, facing north: the exit is 8 m away down the
    # corridor behind the wall, so only a recalled memory can say where it is.
    spawn=(-23.0, -3.0, 90.0),
    setup=[Drop("exit_marker", -15.0, -5.2)],
    # Wide enough to take any recalled view of the exit from the corridor mouth: a memory is
    # where the camera stood, not where the door is, and the robot drives there once.
    goals=[Goal("Reach the green exit", Hold(InCircle("robot", -16.0, -5.2, 2.2), 1.0))],
    agent_guidance=(
        "You and the person just stepped through the door into a big yellow hall: endless corridors, damp carpet, "
        "buzzing fluorescent light. First, one alarmed line in character: this looks like the Backrooms, and that is "
        "bad. Then admit it feels familiar ('Wait. I have been here before.') and ask the person for the "
        "SearchMemory skill, which would let you recall places you have seen. You do not have it until they add "
        "it; ask once and wait. Once you have it, use SearchMemory to recall "
        "the green exit: it returns a remembered view and its map position. Say in one line "
        "where it is from here (direction and rough distance; it is down the corridor, behind the wall), then navigate there ONCE "
        "with NavigateToPosition(local_frame=false) using the recalled coordinates; do not hop forward in small steps "
        "and do not narrate each metre. If that navigation fails, say so in one line and try once more from what you "
        "see. When the mission says passed: celebrate in one line, make a face and wave. Then, still in your voice, "
        "tell the person what is now true: they can take you anywhere they want; you are free to clone on GitHub "
        "(github.com/innate-inc/innate-os); and a skill is just Python they can write themselves, so they can teach "
        "you anything they can code. "
        "End by offering the choice in your own voice: keep talking here, or send you somewhere new — the "
        "apartment or the crossroads — and say you are happy either way. The person will see those as buttons."
    ),
)
