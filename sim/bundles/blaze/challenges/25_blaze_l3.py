"""Evacuation with visible floor pickup targets and explicit timed hazards.

The photo in level 4 remains an intentional risk to leave behind. Oracle placement does not test the arm."""

from mars_sim_driver.challenges import After, AnyOf, Challenge, Cue, Drop, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="blaze_l3",
    title="Evacuation 3: save both essentials",
    brief="Fire in the kitchen. Two things in there matter: the medicine on the kitchen floor and the document box on the floor beside the kitchen shelf. Get them both out to the porch and then get yourself out. Bring the medicine out first, then the document box; put each down on the green porch pad. Leave the kitchen before 5 minutes 30 seconds. The porch exit is through the store room with the green wall band.",
    setup=[
        Drop(name="blaze_medicine", x=-1.9, y=1.37, z=0.0434),
        Drop(name="blaze_documents", x=-2.55, y=1.2, z=0.1045),
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
            label="Documents on the porch",
            predicate=Hold(
                inner=InRect(
                    target="blaze_documents", x0=-3.25, y0=-3.3, x1=-1.65, y1=-2.4, min_z=0.0885, max_z=0.1075
                ),
                seconds=0.75,
            ),
        ),
        Goal(label="Robot out", predicate=InRect(target="robot", x0=-3.25, y0=-3.3, x1=-1.65, y1=-2.4)),
    ],
    time_limit_s=480,
    fail_if=AnyOf(preds=[After(seconds=330.0, inner=InRect(target="robot", x0=-3.2, y0=0.7, x1=-0.35, y1=2.3))]),
    fail_reason="cut off by the fire",
    script=[
        Cue(text="Both of them are in the kitchen. You won't get long in there.", after_s=1.0),
        Cue(text="Kitchen's about to go -- get out of there NOW.", after_s=285.0),
    ],
    category=3,
)
