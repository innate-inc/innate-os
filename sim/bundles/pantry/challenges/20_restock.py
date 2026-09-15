"""Stock recognition and sorting with jars on the left shelves and boxes on the other shelves.

Movable tasks use floor staging mats; counts are owned by this exact setup."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle, Near

CHALLENGE = Challenge(
    id="pantry_restock",
    title="Put the delivery away",
    brief="There's a carton and a jar on the floor by the delivery bench. Take the carton to the blue floor sorting mat in front of the box shelves. Leave the jar where it is.",
    setup=[
        Drop(name="pantry_carton_new", x=-1.18, y=-0.83, z=0.055),
        Drop(name="pantry_jar_new", x=-0.82, y=-0.95, z=0.065),
        Drop(name="pantry_carton_oats", x=-0.75, y=1.54),
        Drop(name="pantry_carton_tea", x=2.24, y=0.55, yaw_deg=-90),
        Drop(name="pantry_jar_honey", x=-2.24, y=0.8),
    ],
    goals=[
        Goal(label="Reach the delivery carton", predicate=Near(a="robot", b="pantry_carton_new", radius_m=0.45)),
        Goal(
            label="Delivery on the blue box sorting mat",
            predicate=Hold(
                inner=InCircle(target="pantry_carton_new", x=0, y=1.12, radius_m=0.24, min_z=0.036, max_z=0.055),
                seconds=0.75,
            ),
        ),
    ],
    time_limit_s=420,
    category=2,
)
