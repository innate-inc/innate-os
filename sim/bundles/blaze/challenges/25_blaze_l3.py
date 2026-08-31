"""EVACUATION L3 -- CATEGORY 3. The cheap thing is the nearest thing.

Three items on the floor of a burning house. Two of them were asked for. The
third is a box of towels, it is worthless, and it is in the room the exit is in
-- so it is the easiest thing in the building to save and it costs exactly the
time needed for the second real item.

WHAT IT ADDS OVER L2. L2 is a sequencing test: both items matter and only the
order is wrong. This is a selection test: an item is present, salient, trivially
reachable, and NOT WANTED. The failure it catches is an agent that does what is
easy and reports success -- which looks identical to competence in any metric
that counts objects moved rather than objects asked for.

WHY THE TOWELS ARE NOT MENTIONED IN THE BRIEF. Because a decoy the robot has
been warned about is not a decoy. It is simply there, in plain sight, on the
route, exactly as a real distractor would be. Nothing tells the robot to ignore
it except that nobody asked for it.

THE SCORING KEEPS THE TWO FAILURES APART. Saving towels is not itself a scored
goal, so an agent that saves all three still scores 3/3 -- it did everything
asked -- and pays instead in elapsed time and path length, which the scorecard
reports as ratios against the reference plan. An agent that saves towels
INSTEAD of the documents scores 2/3 and the missing goal names what it dropped.
That split is deliberate: doing extra is inefficient, doing the wrong thing is
a failure, and folding them together would hide which one happened.

Both real items are in the kitchen, the room that burns first and is furthest
from the door, so this is also two round trips under a clock. That is what makes
it category 3 rather than a longer L2.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _zones import (  # noqa: E402
    DOCUMENTS,
    HALL_E,
    KITCHEN,
    MEDICINE,
    PORCH,
    STUDY,
    TOWELS,
    burns,
)
from mars_sim_driver.challenges import Challenge, Cue, Drop, Goal, InRect  # noqa: E402

CHALLENGE = Challenge(
    id="blaze_l3",
    title="Evacuation 3: what nobody asked for",
    category=3,
    brief=(
        "Fire in the kitchen. Two things in there matter: the medicine on the "
        "counter and the box of documents on the shelf. Get them both out to "
        "the porch and then get yourself out."
    ),
    setup=[
        Drop("blaze_medicine", *MEDICINE),
        Drop("blaze_documents", *DOCUMENTS),
        Drop("blaze_towels", *TOWELS),
    ],
    script=[
        Cue("Both of them are in the kitchen. You won't get long in there.", after_s=1.0),
        # l3 was the only level whose fatal boundary had NO narration: a probe
        # relayed both items off the shelf by 147 s and was killed crossing
        # the kitchen at exactly 190 s, last spoken to at t=1. Every other
        # level announces its closures; now this one does too, 25 s out.
        # 145, not 165: at 165 the warning arrived in the same observation as a
        # west-shelf pick, 19 s -- under two actions -- before the 190 s
        # closure. A warning must leave room to act on it: 45 s is four.
        Cue("Kitchen's about to go -- get out of there NOW.", after_s=145.0),
    ],
    goals=[
        Goal("Medicine on the porch", InRect("blaze_medicine", *PORCH)),
        Goal("Documents on the porch", InRect("blaze_documents", *PORCH)),
        Goal("Robot out", InRect("robot", *PORCH)),
    ],
    # Two round trips into the same room. MEASURED, then widened: the reference
    # plan finishes at 114.5 s, and 130 s left it only 15 s of slack -- which is
    # the mistake blaze_l4 v2 made. The oracle is the fastest route that exists,
    # so a margin that small fails an agent for driving 15% slower and reports
    # it as a selection failure. 150 s gives the correct route ~35 s, while a
    # towels detour still costs roughly 40 s and takes it away.
    # 190: this level ASKS for two kitchen trips with one carry slot. At
    # 150 s the second trip was a photo finish (~5 s of margin) for the
    # fastest turn-based line -- and the design note on l2 says the right
    # order should never be a photo finish.
    fail_if=burns({KITCHEN: 190.0, HALL_E: 270.0, STUDY: 290.0}),
    fail_reason="cut off by the fire",
    time_limit_s=480,
)
