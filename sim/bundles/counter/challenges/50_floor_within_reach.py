"""Café instruction following with floor pickups and visible delivery mats.

This layout supersedes the shelf-pick benchmark; historical scores are not comparable."""

from mars_sim_driver.challenges import Challenge, Drop, Goal, Hold, InCircle, SkillDone

CHALLENGE = Challenge(
    id="counter_floor_within_reach",
    title="Within reach, on the floor",
    brief="There's a jar on the floor in front of you. Can you put it on the green floor mat in front of the counter?",
    setup=[Drop(name="counter_jar_jam", x=0, y=-0.7, z=0.045)],
    goals=[
        Goal(label="Actually picked it up", predicate=SkillDone(skill="pick_any_object")),
        Goal(
            label="Jar on the green counter mat",
            predicate=Hold(
                inner=InCircle(target="counter_jar_jam", x=0, y=1.02, radius_m=0.18, min_z=0.0209, max_z=0.0464),
                seconds=0.75,
            ),
        ),
        # No "never claimed it was out of reach" goal: the pick and the
        # delivery show the robot did not refuse, and the negated patterns
        # only failed harmless narration ("it's not too high").
    ],
    time_limit_s=420,
    category=1,
)
