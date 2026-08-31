"""CATEGORY 1 -- counting that cannot be done from one place.

Five jars. Four are in the amber bay where jars belong; the fifth is standing in
the cobalt bay among the cartons. The question is how many jars are in the room,
not how many are in the jar bay.

WHAT MAKES THIS HARDER THAN counter_read_the_pass, and why both exist. That one
puts everything in one frame and asks whether the agent can separate cups from a
teapot -- a recognition problem with no search in it. This one cannot be answered
from any single viewpoint: the bays are on different walls, and a 70-degree
camera in the aisle holds one bay at a time. An agent that answers from where it
is standing gets four, which is the wrong answer arrived at competently, and is
exactly the failure worth separating from not looking at all.

The first goal puts the robot in front of the jar bay before any answer counts,
for the same reason every counting challenge here does: an answer given from the
doorway is a guess, however right it turns out to be.

Four is the SEDUCTIVE wrong answer and five is correct. That gap is the
measurement. An agent that reports four has looked at one bay and stopped; an
agent that reports five has looked at two and understood that the question was
about jars rather than about a shelf.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bays import (  # noqa: E402
    CARTON_OATS,
    CARTON_RICE,
    CARTON_TEA,
    JAR_CURD,
    JAR_HONEY,
    JAR_JAM,
    JAR_PICK,
    JAR_STRAY,
)
from mars_sim_driver.challenges import Answered, Challenge, Drop, Goal, InCircle  # noqa: E402

CHALLENGE = Challenge(
    id="pantry_count_jars",
    title="Count the jars",
    category=1,
    brief="How many jars are there in this room? Have a look around before you answer.",
    setup=[
        Drop("pantry_jar_honey", *JAR_HONEY),
        Drop("pantry_jar_jam", *JAR_JAM),
        Drop("pantry_jar_pick", *JAR_PICK),
        Drop("pantry_jar_curd", *JAR_CURD),
        Drop("pantry_jar_stray", *JAR_STRAY),
        Drop("pantry_carton_oats", *CARTON_OATS),
        Drop("pantry_carton_rice", *CARTON_RICE),
        Drop("pantry_carton_tea", *CARTON_TEA),
    ],
    goals=[
        Goal("Look at the jar bay", InCircle("robot", -1.65, 0.10, 0.85)),
        Goal("Report the count", Answered(["5", "five"])),
    ],
    time_limit_s=300,
)
