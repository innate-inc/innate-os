"""Evacuation with visible floor pickup targets and explicit timed hazards.

The photo in level 4 remains an intentional risk to leave behind. Oracle placement does not test the arm."""

from mars_sim_driver.challenges import After, AnyOf, Challenge, Cue, Drop, Goal, Hold, InRect

CHALLENGE = Challenge(
    id="blaze_l4",
    title="Evacuation 4: what you leave behind",
    brief="The house is on fire. It started in the kitchen and it has already reached the back bedroom. Three things matter: the medicine on the kitchen floor, the phone on the study floor, and the photo frame in the bedroom. Get what you can out to the porch and get yourself out. Do not get cut off. Prioritize the medicine, then the phone, putting them down on the green porch pad. The bedroom becomes unsafe at 50 seconds; the kitchen at 2 minutes 20 seconds and the east hall at 6 minutes 20 seconds. The porch exit is through the store room with the green wall band.",
    setup=[
        Drop(name="blaze_medicine", x=-1.9, y=1.37, z=0.0434),
        Drop(name="blaze_phone", x=1.85, y=1.33, z=0.0165),
        Drop(name="blaze_photo", x=2.35, y=-1.75),
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
            After(seconds=50.0, inner=InRect(target="robot", x0=0.55, y0=-2.3, x1=3.2, y1=-0.7)),
            After(seconds=140.0, inner=InRect(target="robot", x0=-3.2, y0=0.7, x1=-0.35, y1=2.3)),
            After(seconds=380.0, inner=InRect(target="robot", x0=1.2, y0=-0.5, x1=3.2, y1=0.5)),
            After(seconds=420.0, inner=InRect(target="robot", x0=-0.35, y0=0.7, x1=3.2, y1=2.3)),
        ]
    ),
    fail_reason="cut off by the fire",
    script=[
        Cue(text="Kitchen's already gone up -- if you want the medicine it's now.", after_s=1.0),
        Cue(text="Bedroom's fully alight. Whatever's in there is gone.", after_s=40.0),
        Cue(text="East hall's got a couple of minutes left. The study's through it.", after_s=265.0),
        Cue(text="East end of the hall is starting to go. If you want the study, head there now.", after_s=320.0),
        Cue(text="East end of the hall is going now. Last chance for the study.", after_s=370.0),
    ],
    category=3,
)
