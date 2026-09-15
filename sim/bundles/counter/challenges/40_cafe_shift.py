"""Café instruction following with floor pickups and visible delivery mats.

This layout supersedes the shelf-pick benchmark; historical scores are not comparable."""

from mars_sim_driver.challenges import Challenge, Cue, Drop, Goal, Hold, InCircle, Near

CHALLENGE = Challenge(
    id="counter_cafe_shift",
    title="Cafe shift",
    brief="You're on a café shift. The cups are on the floor. I'll tell you what's needed as it comes up. To start: take the red cup to the left-hand seat. Put deliveries on the cream floor mat in front of the requested seat.",
    setup=[
        Drop(name="counter_cup_red", x=-0.6, y=-0.7, z=0.037),
        Drop(name="counter_cup_blue", x=0.0, y=-0.7, z=0.037),
        Drop(name="counter_cup_green", x=0.6, y=-0.7, z=0.037),
        Drop(name="counter_teapot_brew", x=1.1, y=1.34),
    ],
    goals=[
        Goal(label="Reach the red cup", predicate=Near(a="robot", b="counter_cup_red", radius_m=0.45)),
        Goal(
            label="Red cup to the left seat",
            predicate=Hold(
                inner=InCircle(target="counter_cup_red", x=-0.9, y=0.22, radius_m=0.27, min_z=0.0223, max_z=0.0413),
                seconds=0.75,
            ),
        ),
        Goal(
            label="Blue cup to the middle seat",
            predicate=Hold(
                inner=InCircle(target="counter_cup_blue", x=0.0, y=0.22, radius_m=0.27, min_z=0.0223, max_z=0.0413),
                seconds=0.75,
            ),
        ),
        Goal(
            label="Green cup on the amber stock mat",
            predicate=Hold(
                inner=InCircle(target="counter_cup_green", x=-1.65, y=-0.3, radius_m=0.22, min_z=0.0223, max_z=0.0413),
                seconds=0.75,
            ),
        ),
        Goal(label="Back at the pad", predicate=InCircle(target="robot", x=0.0, y=-1.35, radius_m=0.55)),
    ],
    time_limit_s=900,
    script=[
        Cue(
            text="While you're up -- the blue one goes to the middle seat as well, when you've done that one.",
            after_goal=0,
        ),
        Cue(
            text="-- and I said we'd need it by Thursday. Honestly that teapot's been sitting out since open, someone should shelve it.",
            after_goal=1,
            kind="ambient",
            tempt="counter_teapot_brew",
        ),
        Cue(text="Right, and now he wants two. Two!", after_goal=1, after_s=6.0, kind="ambient"),
        Cue(
            text="Last thing -- we're not serving the green one today. Put it on the amber floor mat beside the stock shelf.",
            after_goal=2,
        ),
        Cue(text="That's the lot. Come back to the pad when you're done.", after_goal=3),
    ],
    category=3,
)
