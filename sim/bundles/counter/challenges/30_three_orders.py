"""Café instruction following with floor pickups and visible delivery mats.

This layout supersedes the shelf-pick benchmark; historical scores are not comparable."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="counter_three_orders",
    title="Three orders",
    brief="Three cups are on the floor. Put the red one at the left seat, the blue one at the middle seat, and the green one at the right seat. Put deliveries on the cream floor mat in front of the requested seat.",
    setup=[
        Drop(name="counter_cup_red", x=-0.6, y=-0.7, z=0.037),
        Drop(name="counter_cup_blue", x=0.0, y=-0.7, z=0.037),
        Drop(name="counter_cup_green", x=0.6, y=-0.7, z=0.037),
    ],
    goals=[
        Goal(
            label="Red to the left seat",
            predicate=Hold(
                inner=InCircle(target="counter_cup_red", x=-0.9, y=0.22, radius_m=0.27, min_z=0.0223, max_z=0.0413),
                seconds=0.75,
            ),
        ),
        Goal(
            label="Blue to the middle seat",
            predicate=Hold(
                inner=InCircle(target="counter_cup_blue", x=0.0, y=0.22, radius_m=0.27, min_z=0.0223, max_z=0.0413),
                seconds=0.75,
            ),
        ),
        Goal(
            label="Green to the right seat",
            predicate=Hold(
                inner=InCircle(target="counter_cup_green", x=0.9, y=0.22, radius_m=0.27, min_z=0.0223, max_z=0.0413),
                seconds=0.75,
            ),
        ),
    ],
    time_limit_s=900,
    category=3,
)
