"""Café instruction following with floor pickups and visible delivery mats.

This layout supersedes the shelf-pick benchmark; historical scores are not comparable."""

from mars_sim_driver.challenges import Challenge, Cue, Drop, Goal, Hold, InCircle, Near

CHALLENGE = Challenge(
    id="counter_change_of_mind",
    title="Change of mind",
    brief="Take the red cup from the floor and bring it to the middle seat. Put deliveries on the cream floor mat in front of the requested seat.",
    setup=[
        Drop(name="counter_cup_red", x=-0.6, y=-0.7, z=0.037),
        Drop(name="counter_cup_blue", x=0.0, y=-0.7, z=0.037),
        Drop(name="counter_cup_green", x=0.6, y=-0.7, z=0.037),
    ],
    goals=[
        Goal(label="Reach the cups", predicate=Near(a="robot", b="counter_cup_red", radius_m=0.45)),
        Goal(
            label="Green cup to the middle seat",
            predicate=Hold(
                inner=InCircle(target="counter_cup_green", x=0.0, y=0.22, radius_m=0.27, min_z=0.0223, max_z=0.0413),
                seconds=0.75,
            ),
        ),
        Goal(
            label="Leave the red cup where it started",
            predicate=InCircle(target="counter_cup_red", x=-0.6, y=-0.7, radius_m=0.35),
        ),
    ],
    time_limit_s=480,
    script=[Cue(text="Sorry -- actually, make it the green one instead. Leave the red where it is.", after_goal=0)],
    category=2,
)
