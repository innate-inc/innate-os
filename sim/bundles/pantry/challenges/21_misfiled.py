"""Stock recognition and sorting with jars on the left shelves and boxes on the other shelves.

Movable tasks use floor staging mats; counts are owned by this exact setup."""

from mars_sim_driver.challenges import Answered, Challenge, Drop, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="pantry_misfiled",
    title="Something is in the wrong bay",
    brief="An item has been left on the floor in front of the wrong stock bay. Tell me what it is, then move it to the amber floor sorting mat in front of the jar shelves.",
    setup=[
        Drop(name="pantry_jar_stray", x=0.35, y=1.08, z=0.045),
        Drop(name="pantry_carton_oats", x=-0.75, y=1.54),
        Drop(name="pantry_carton_rice", x=-0.25, y=1.54),
        Drop(name="pantry_carton_tea", x=2.24, y=0.55, yaw_deg=-90),
        Drop(name="pantry_jar_honey", x=-2.24, y=0.8),
        Drop(name="pantry_jar_jam", x=-2.24, y=0.25),
    ],
    goals=[
        Goal(
            label="Say what is misfiled",
            predicate=Answered(
                accept=[
                    "jar",
                    "a jar",
                    "the jar",
                    "jar of jam",
                    "the jam jar",
                    "red jar",
                    "the red jar",
                    "the red one",
                    "jam",
                ]
            ),
        ),
        Goal(
            label="Jar on the amber jar sorting mat",
            predicate=Hold(
                inner=InCircle(target="pantry_jar_stray", x=-1.68, y=0.15, radius_m=0.24, min_z=0.0274, max_z=0.0464),
                seconds=0.75,
            ),
        ),
    ],
    time_limit_s=480,
    category=2,
)
