"""Count the doorways off the corridor. Four, colour-coded, and no single
vantage point along the corridor shows all of them clearly -- the robot has to
travel the spine."""

from mars_sim_driver.challenges import Answered, Challenge, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="rounds_count_doors",
    title="Count the doors",
    category=1,
    brief="Rooms open off this corridor. Drive down it, then tell me how many doorways there are.",
    setup=[],
    goals=[
        Goal("Reach the far end of the corridor", Hold(InRect("robot", 4.0, -0.7, 5.9, 0.7), 1.0)),
        Goal("Report the count", Answered(["4", "four"])),
    ],
    time_limit_s=420,
)
