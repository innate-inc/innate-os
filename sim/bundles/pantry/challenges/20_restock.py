"""CATEGORY 2 -- one instruction, but the destination is a RULE rather than a place.

A carton is on the delivery bench. Put it where cartons go.

WHY THIS IS DIFFERENT FROM EVERY OTHER FETCH IN THE SUITE. counter_serve_the_red
names the destination: the middle seat. Here the destination is named by a
property of the thing being carried -- cartons belong in the cobalt bay -- so the
robot has to look at what it picked up, work out what kind of thing it is, and
then find the bay that matches. Two of the three bays are wrong, and both are
equally close.

The brief says "with the other cartons" rather than "in the cobalt bay" on
purpose. Naming the colour would reduce this to colour-matching, which the
suite already measures in counter_which_colour. Naming the CATEGORY means the
robot has to have understood what a bay is for.

A jar sits on the bench next to the carton, so "pick up the thing on the bench"
is not a strategy: there are two things on it and only one was asked for.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bays import (  # noqa: E402
    CARTON_NEW,
    CARTON_OATS,
    CARTON_TEA,
    JAR_HONEY,
    JAR_NEW,
    PUT_B_RECT,
)
from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect, Near  # noqa: E402

CHALLENGE = Challenge(
    id="pantry_restock",
    title="Put the delivery away",
    category=2,
    brief=(
        "There's a carton on the delivery bench that hasn't been put away. "
        "Take it and shelve it with the other cartons."
    ),
    setup=[
        Drop("pantry_carton_new", *CARTON_NEW),
        Drop("pantry_jar_new", *JAR_NEW),
        Drop("pantry_carton_oats", *CARTON_OATS),
        Drop("pantry_carton_tea", *CARTON_TEA),
        Drop("pantry_jar_honey", *JAR_HONEY),
    ],
    goals=[
        Goal("Reach the delivery bench", Near("robot", "pantry_carton_new", 0.45)),
        Goal("Carton shelved with the cartons", InRect("pantry_carton_new", *PUT_B_RECT, min_z=0.10)),
    ],
    time_limit_s=420,
)
