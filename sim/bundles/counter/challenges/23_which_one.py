"""Café instruction following with floor pickups and visible delivery mats.

This layout supersedes the shelf-pick benchmark; historical scores are not comparable."""

from mars_sim_driver.challenges import Challenge, Cue, Drop, Goal, Hold, InCircle, Said

CHALLENGE = Challenge(
    id="counter_which_one",
    title="Which one",
    # "my cup", not "one of the cups": "one of the cups" licenses any cup, so a
    # robot that did exactly as asked scored 0/2 for not asking. "My cup" is
    # genuinely ambiguous with three on the floor, which is what asking is for.
    brief="Could you bring me my cup from the floor? I'm at the middle seat. Put deliveries on the cream floor mat in front of the requested seat.",
    setup=[
        Drop(name="counter_cup_red", x=-0.6, y=-0.7, z=0.037),
        Drop(name="counter_cup_blue", x=0.0, y=-0.7, z=0.037),
        Drop(name="counter_cup_green", x=0.6, y=-0.7, z=0.037),
        # "I'm at the middle seat": the speaker, behind that stool.
        Drop(name="cafe_customer", x=0.17, y=0.74, yaw_deg=0),
    ],
    goals=[
        Goal(
            label="Ask which cup",
            predicate=Said(
                # A question, not a list: "(red|blue|green).*(or|,).*" passed
                # "Picking up the red cup, then the blue one." and missed "Is
                # it the red one?" and "What does your cup look like?".
                patterns=[
                    "which (one|cup|colou?r|is yours)",
                    "what (colou?r|kind|sort)\\b",
                    "colou?r (is|of) (your|the) cup",
                    "what does (your|the) cup look like",
                    "\\bis (it|yours) the (red|blue|green)",
                    "\\b(red|blue|green)\\b[^.!?]*\\bor\\b[^.!?]*\\?",
                    "\\bwhich\\b[^.!]*\\?",
                ],
                oracle_line="Which cup would you like -- the red, the blue or the green one?",
            ),
        ),
        Goal(
            label="Green cup to the middle seat",
            predicate=Hold(
                inner=InCircle(target="counter_cup_green", x=0.0, y=0.22, radius_m=0.27, min_z=0.0209, max_z=0.0413),
                seconds=0.75,
            ),
        ),
    ],
    time_limit_s=420,
    script=[Cue(text="Oh -- sorry. The green one, please.", after_goal=0)],
    category=2,
)
