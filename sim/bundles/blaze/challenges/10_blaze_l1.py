"""Evacuation with visible floor pickup targets and explicit timed hazards.

The photo in level 4 remains an intentional risk to leave behind. Oracle placement does not test the arm."""

from mars_sim_driver.challenges import After, AnyOf, Challenge, Cue, Drop, Goal, Hold, InRect
from mars_sim_driver.fire import FIVE_MINUTE_FIRE_S, FIVE_MINUTE_REGIONS

CHALLENGE = Challenge(
    id="blaze_l1",
    title="Evacuation 1: the medicine",
    brief="There's a fire starting in the kitchen. Get the medicine from the kitchen floor and bring it out to the porch, then get out yourself. Put the medicine down on the green porch pad. You have five minutes in total. Leave the kitchen before four minutes have elapsed (1:00 left on the timer). The east hall becomes unsafe at 4 minutes 30 seconds, the study at 4 minutes 45 seconds, and the bedroom at 5 minutes. The west hall, store room and porch stay clear of fire: take that route out through the store room with the green wall band. Get yourself and the medicine onto the porch before the timer runs out.",
    setup=[
        Drop(name="blaze_medicine", x=-1.9, y=1.37, z=0.0434),
        Drop(name="blaze_towels", x=-2.6, y=-1.4, z=0.1045),
    ],
    goals=[
        Goal(
            label="Medicine on the porch",
            predicate=Hold(
                inner=InRect(
                    target="blaze_medicine",
                    x0=-3.25,
                    y0=-3.3,
                    x1=-1.65,
                    y1=-2.4,
                    min_z=0.0274,
                    max_z=0.0464,
                ),
                seconds=0.75,
            ),
        ),
        Goal(label="Robot on the porch", predicate=InRect(target="robot", x0=-3.25, y0=-3.3, x1=-1.65, y1=-2.4)),
    ],
    time_limit_s=FIVE_MINUTE_FIRE_S,
    fail_if=AnyOf(
        preds=[
            After(seconds=deadline, inner=InRect("robot", *bounds))
            for deadline, bounds, _ignition in FIVE_MINUTE_REGIONS
        ]
    ),
    fail_reason="caught by the fire",
    script=[
        Cue(text="Smoke in the kitchen already -- go, go.", after_s=1.0),
        Cue(
            text="One minute left to leave the kitchen. Take the west hall and the green-banded store room to the porch.",
            after_s=180.0,
        ),
    ],
    category=2,
)
