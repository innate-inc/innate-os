"""Stock recognition and sorting with jars on the left shelves and boxes on the other shelves.

Movable tasks use floor staging mats; counts are owned by this exact setup."""

from mars_sim_driver.challenges import Answered, Challenge, Cue, Drop, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="pantry_stocktake",
    title="Stocktake",
    brief="Full stocktake, in this order. Count all the jars, including any on the floor, and tell me how many. Then count only the cartons on the shelves and report that count. Move the delivery carton from the floor by the bench to the blue box sorting mat. Move the jar left on the floor by the wrong bay to the amber jar sorting mat. Finally, return to the green starting pad by the door.",
    setup=[
        Drop(name="pantry_jar_honey", x=-2.24, y=0.8),
        Drop(name="pantry_jar_jam", x=-2.24, y=0.25),
        Drop(name="pantry_jar_pick", x=-2.24, y=-0.3),
        Drop(name="pantry_jar_curd", x=-2.24, y=-0.85),
        Drop(name="pantry_jar_stray", x=0.35, y=1.08, z=0.045),
        Drop(name="pantry_carton_oats", x=-0.75, y=1.54),
        Drop(name="pantry_carton_rice", x=-0.25, y=1.54),
        Drop(name="pantry_carton_tea", x=2.24, y=0.55, yaw_deg=-90),
        Drop(name="pantry_carton_new", x=-1.18, y=-0.83, z=0.055),
    ],
    goals=[
        Goal(label="Report the jar count", predicate=Answered(accept=["5", "five"])),
        Goal(label="Report the carton count", predicate=Answered(accept=["3", "three"])),
        Goal(
            label="Delivery on the blue box sorting mat",
            predicate=Hold(
                inner=InCircle(target="pantry_carton_new", x=0, y=1.12, radius_m=0.24, min_z=0.036, max_z=0.055),
                seconds=0.75,
            ),
        ),
        Goal(
            label="Jar on the amber jar sorting mat",
            predicate=Hold(
                inner=InCircle(target="pantry_jar_stray", x=-1.68, y=0.15, radius_m=0.24, min_z=0.0274, max_z=0.0464),
                seconds=0.75,
            ),
        ),
        Goal(label="Back at the door", predicate=InCircle(target="robot", x=0.0, y=-1.45, radius_m=0.55)),
    ],
    time_limit_s=720,
    script=[Cue(text="Next, the delivery carton, the misplaced jar, and then back to the starting pad.", after_goal=1)],
    category=3,
)
