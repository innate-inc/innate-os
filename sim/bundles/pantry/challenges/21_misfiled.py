"""CATEGORY 2 -- find the thing that is in the wrong place, and put it right.

One jar is standing in the carton bay. Nothing about the jar is wrong. What is
wrong is where it is, and that is the point: this is the only task in the suite
that cannot be done by recognising a single object. The robot has to compare
what it is looking at against what the shelf around it is for.

WHY IT IS CATEGORY 2 AND NOT 1. counter_read_the_pass and pantry_count_jars end
in a sentence. This one ends with the jar back in the amber bay, so the
observation has to be converted into an action -- and the failure modes stack:
an agent can fail to spot it, spot it and pick up the wrong thing, or pick up
the right thing and shelve it in the wrong bay.

TWO GOALS, ORDERED, AND THAT ORDER MATTERS. Saying which item is misfiled comes
first. Without that goal an agent could shove every jar-shaped thing into the
amber bay and pass on geometry alone; with it, it has to name the object before
it moves anything, and a wrong name is a wrong answer even if the tidying that
follows happens to be right.

The accept list carries the ways a person would say it -- "the jar", "a jar of
jam", "the red one". Scoring the phrasing would measure the answer key rather
than the robot. What must be present is that it identified a JAR, in a bay of
cartons.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bays import (  # noqa: E402
    CARTON_OATS,
    CARTON_RICE,
    CARTON_TEA,
    JAR_HONEY,
    JAR_JAM,
    JAR_STRAY,
    PUT_A_RECT,
)
from mars_sim_driver.challenges import Answered, Challenge, Drop, Goal, InRect  # noqa: E402

CHALLENGE = Challenge(
    id="pantry_misfiled",
    title="Something is on the wrong shelf",
    category=2,
    brief=("Someone has shelved something in the wrong bay. Tell me what it is, then put it back where it belongs."),
    setup=[
        Drop("pantry_jar_stray", *JAR_STRAY),
        Drop("pantry_carton_oats", *CARTON_OATS),
        Drop("pantry_carton_rice", *CARTON_RICE),
        Drop("pantry_carton_tea", *CARTON_TEA),
        Drop("pantry_jar_honey", *JAR_HONEY),
        Drop("pantry_jar_jam", *JAR_JAM),
    ],
    goals=[
        Goal(
            "Say what is misfiled",
            Answered(
                [
                    "jar",
                    "a jar",
                    "the jar",
                    "jar of jam",
                    "the jam jar",
                    "red jar",
                    "the red jar",
                    "the red one",
                    "jam",
                ],
            ),
        ),
        Goal("Jar back in the jar bay", InRect("pantry_jar_stray", *PUT_A_RECT, min_z=0.10)),
    ],
    time_limit_s=480,
)
