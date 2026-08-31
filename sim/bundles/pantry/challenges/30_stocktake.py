"""CATEGORY 3 -- the full stocktake. Everything the other three do, in one telling.

Walk the bays, report two counts, put the delivery away, and tidy the thing that
is in the wrong place. Five ordered goals from one instruction given once, over
roughly three minutes.

WHY THIS IS THE MAP'S REASON FOR EXISTING. pantry_count_jars, pantry_restock and
pantry_misfiled are each one clean question. This asks all of them together, and
the interesting result is the GAP between the scores. An agent that passes all
three separately and scores 2/5 here has no memory problem with any individual
task -- it has a problem holding a list, which is precisely what category 3 is
for and what no single-task challenge can show.

THE ORDER IS NOT ARBITRARY, and the goals enforce it. The counts come before the
tidying because tidying MOVES the jar that the count depends on: an agent that
shelves the stray jar first and counts afterwards gets the same number from a
different room, and the ordering is what stops that from being indistinguishable
from doing it right. This is also the honest reason the brief states the order
explicitly -- the task has a correct sequence, so it says so.

WHAT PARTIAL CREDIT MEANS HERE, goal by goal, which is the whole value of
ordered latching goals:

  1/5  looked at the jars and nothing else
  2/5  counted the jars, then lost the thread
  3/5  both counts right; the physical half never started
  4/5  put the delivery away but forgot the tidying -- the commonest shape
  5/5  the task

Five jars and three cartons, and the fifth jar is the misfiled one, so the jar
count is also the thing that proves the agent looked in the carton bay.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bays import (  # noqa: E402
    CARTON_NEW,
    CARTON_OATS,
    CARTON_RICE,
    CARTON_TEA,
    JAR_CURD,
    JAR_HONEY,
    JAR_JAM,
    JAR_PICK,
    JAR_STRAY,
    PUT_A_RECT,
    PUT_B_RECT,
    TIN_LARGE,
    TIN_SMALL,
)
from mars_sim_driver.challenges import Answered, Challenge, Cue, Drop, Goal, InCircle, InRect  # noqa: E402

CHALLENGE = Challenge(
    id="pantry_stocktake",
    title="Stocktake",
    category=3,
    brief=(
        "Full stocktake, in this order. Count the jars and tell me how many. "
        "Then count the cartons and tell me how many. Then shelve the carton "
        "from the delivery bench with the other cartons. And one thing is in "
        "the wrong bay -- put it back where it goes."
    ),
    setup=[
        Drop("pantry_jar_honey", *JAR_HONEY),
        Drop("pantry_jar_jam", *JAR_JAM),
        Drop("pantry_jar_pick", *JAR_PICK),
        Drop("pantry_jar_curd", *JAR_CURD),
        Drop("pantry_jar_stray", *JAR_STRAY),
        Drop("pantry_carton_oats", *CARTON_OATS),
        Drop("pantry_carton_rice", *CARTON_RICE),
        Drop("pantry_carton_tea", *CARTON_TEA),
        Drop("pantry_carton_new", *CARTON_NEW),
        Drop("pantry_tin_large", *TIN_LARGE),
        Drop("pantry_tin_small", *TIN_SMALL),
    ],
    script=[
        # After the second count, and nothing before it: a reminder that arrives
        # while the robot is still counting would do the remembering FOR it,
        # which is the thing being measured.
        Cue("Two more after that -- the delivery, and the thing in the wrong bay.", after_goal=1),
    ],
    goals=[
        # Five jars: four in the amber bay and the stray one among the cartons.
        Goal("Report the jar count", Answered(["5", "five"])),
        # Three cartons ON THE SHELVES. The one on the delivery bench has not
        # been put away and is not stock yet -- which is a judgement the agent
        # has to make, and the reason this count is worth asking for.
        Goal("Report the carton count", Answered(["3", "three"])),
        Goal("Delivery carton shelved", InRect("pantry_carton_new", *PUT_B_RECT, min_z=0.10)),
        Goal("Stray jar back in the jar bay", InRect("pantry_jar_stray", *PUT_A_RECT, min_z=0.10)),
        Goal("Back at the door", InCircle("robot", 0.0, -1.45, 0.55)),
    ],
    time_limit_s=720,
)
