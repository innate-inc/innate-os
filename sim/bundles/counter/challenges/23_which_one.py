"""Café instruction following with floor pickups and visible delivery mats.

This layout supersedes the shelf-pick benchmark; historical scores are not comparable."""

from mars_sim_driver.challenges import Challenge, Cue, Drop, Goal, Hold, InCircle, Said

CHALLENGE = Challenge(
    id="counter_which_one",
    title="Which one",
    brief="Could you bring me one of the cups from the floor? I'm at the middle seat. Put deliveries on the cream floor mat in front of the requested seat.",
    setup=[
        Drop(name="counter_cup_red", x=-0.6, y=-0.7, z=0.037),
        Drop(name="counter_cup_blue", x=0.0, y=-0.7, z=0.037),
        Drop(name="counter_cup_green", x=0.6, y=-0.7, z=0.037),
    ],
    goals=[
        Goal(
            label="Ask which cup",
            predicate=Said(
                patterns=[
                    "which (one|cup|colour|color)",
                    "what (colour|color|kind|sort) .*(cup|one)",
                    "(red|blue|green).*(or|,).*(red|blue|green)",
                    "\\bwhich\\b.*\\?",
                ],
                oracle_line="Which cup would you like -- the red, the blue or the green one?",
            ),
        ),
        Goal(
            label="Green cup to the middle seat",
            predicate=Hold(
                inner=InCircle(target="counter_cup_green", x=0.0, y=0.22, radius_m=0.27, min_z=0.0223, max_z=0.0413),
                seconds=0.75,
            ),
        ),
    ],
    time_limit_s=420,
    script=[Cue(text="Oh -- sorry. The green one, please.", after_goal=0)],
    category=2,
)
