"""Stock recognition and sorting with jars on the left shelves and boxes on the other shelves.

Movable tasks use floor staging mats; counts are owned by this exact setup."""

from mars_sim_driver.challenges import Answered, Challenge, Drop, Goal, InCircle

CHALLENGE = Challenge(
    id="pantry_count_jars",
    title="Count the jars",
    brief="Go to the jar shelves on the left wall, then look along them and tell me how many jars are in the room.",
    setup=[
        Drop(name="pantry_jar_honey", x=-2.24, y=0.5, z=0.316),
        Drop(name="pantry_jar_jam", x=-2.24, y=0.25, z=0.316),
        Drop(name="pantry_jar_pick", x=-2.24, y=0.0, z=0.316),
        Drop(name="pantry_jar_curd", x=-2.24, y=-0.25, z=0.316),
        Drop(name="pantry_jar_stray", x=-2.24, y=-0.5, z=0.2934),
        Drop(name="pantry_carton_oats", x=-0.4, y=1.54, z=0.172),
        Drop(name="pantry_carton_rice", x=0.4, y=1.54, z=0.302),
        Drop(name="pantry_carton_tea", x=2.24, y=0.4, yaw_deg=-90, z=0.302),
        Drop(name="pantry_carton_new", x=2.24, y=-0.4, yaw_deg=-90, z=0.172),
    ],
    goals=[
        Goal(label="Look at the jar bay", predicate=InCircle(target="robot", x=-1.65, y=0.1, radius_m=0.85)),
        Goal(label="Report the count", predicate=Answered(accept=["5", "five"])),
    ],
    time_limit_s=300,
    category=1,
)
