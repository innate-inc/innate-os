"""Café instruction following with floor pickups and visible delivery mats.

This layout supersedes the shelf-pick benchmark; historical scores are not comparable."""

from mars_sim_driver.challenges import Challenge, Cue, Drop, Goal, Hold, InCircle, Near

CHALLENGE = Challenge(
    id="counter_second_order",
    title="Second order",
    brief="Take the red cup from the floor over to the left seat, please. Put deliveries on the cream floor mat in front of the requested seat.",
    setup=[
        Drop(name="counter_cup_red", x=-0.6, y=-0.7, z=0.037),
        Drop(name="counter_cup_blue", x=0.0, y=-0.7, z=0.037),
        Drop(name="counter_cup_green", x=0.6, y=-0.7, z=0.037),
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
            label="Blue cup to the right seat",
            predicate=Hold(
                inner=InCircle(target="counter_cup_blue", x=0.9, y=0.22, radius_m=0.27, min_z=0.0223, max_z=0.0413),
                seconds=0.75,
            ),
        ),
    ],
    time_limit_s=600,
    script=[
        Cue(
            text="Sorry to interrupt -- when you've done that, could you bring the blue one to the right-hand seat as well? No rush.",
            after_goal=0,
        )
    ],
    category=2,
)
