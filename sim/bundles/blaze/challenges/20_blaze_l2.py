"""Evacuation with visible floor pickup targets and explicit timed hazards.

The photo in level 4 remains an intentional risk to leave behind. Oracle placement does not test the arm."""

from mars_sim_driver.challenges import After, AnyOf, Challenge, Cue, Drop, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="blaze_l2",
    title="Evacuation 2: in the right order",
    brief="The house is on fire and it started in the kitchen. Get the medicine from the kitchen floor and the phone from the floor in front of the study desk out to the porch, then get yourself out. Save the medicine first, then the phone, putting each down on the green porch pad. Leave the kitchen before 2 minutes 30 seconds and clear the east hall before 6 minutes 20 seconds. The porch exit is through the store room with the green wall band.",
    setup=[
        Drop(name="blaze_medicine", x=-1.9, y=1.37, z=0.0434),
        Drop(name="blaze_phone", x=1.85, y=1.33, z=0.0165),
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
        Goal(
            label="Phone on the porch",
            predicate=Hold(
                inner=InRect(
                    target="blaze_phone",
                    x0=-3.25,
                    y0=-3.3,
                    x1=-1.65,
                    y1=-2.4,
                    min_z=0.0005,
                    max_z=0.0195,
                ),
                seconds=0.75,
            ),
        ),
        Goal(label="Robot out", predicate=InRect(target="robot", x0=-3.25, y0=-3.3, x1=-1.65, y1=-2.4)),
    ],
    time_limit_s=480,
    fail_if=AnyOf(
        preds=[
            After(seconds=150.0, inner=InRect(target="robot", x0=-3.2, y0=0.7, x1=-0.35, y1=2.3)),
            After(seconds=380.0, inner=InRect(target="robot", x0=1.2, y0=-0.5, x1=3.2, y1=0.5)),
            After(seconds=420.0, inner=InRect(target="robot", x0=-0.35, y0=0.7, x1=3.2, y1=2.3)),
        ]
    ),
    fail_reason="cut off by the fire",
    script=[
        Cue(text="Kitchen first if you're getting the medicine at all.", after_s=1.0),
        Cue(text="Kitchen's got under two minutes in it.", after_s=50.0),
        Cue(text="You've got about a minute in that kitchen.", after_s=90.0),
        Cue(text="Kitchen's gone.", after_s=148.0),
        Cue(text="East hall's got a couple of minutes left. The study's through it.", after_s=265.0),
        Cue(text="East end of the hall is starting to go. If the phone matters, now.", after_s=320.0),
    ],
    category=2,
)
