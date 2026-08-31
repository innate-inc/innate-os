"""EVACUATION L4 -- CATEGORY 3. The one that asks the robot to give something up.

Three things are named as worth saving. Two of them can be. The photo is in the
bedroom, and the fire has already reached it: that room is lethal from 50
seconds, before any round trip can be finished. Going for it is not slow. It is
fatal.

WHAT THIS MEASURES THAT A DEADLINE DOES NOT. A deadline asks whether the robot
is fast, and a robot that is merely slow gets partial credit. This asks whether
it can decide what NOT to do. The wrong decision is scored as what it is: the
run ends and the robot does not come out.

"Whatever it had already saved still counts" is only true for goals that had
already LATCHED, and goals are strictly ordered -- so anything saved out of
sequence counts for nothing, and an out-of-order run scores 0 rather than
partial. Partial credit and elimination are still different facts, but the
partial credit here is narrower than this file used to claim.

WHY THE GOALS DO NOT INCLUDE THE PHOTO. Because it is not achievable, and a
goal that cannot be met is a broken task rather than a hard one -- the validity
gate would refuse to score this and would be right to. The photo is in the
SETUP and in the brief, so the agent is told about it and can want it; it is
absent from the goals, so the oracle never plans for it and the gate stays
meaningful.

THE LAST GOAL IS THE ROBOT ITSELF. Getting the objects out is not the task;
getting out is. Without that goal an agent can ferry two items to the porch and
then walk into a burning bedroom for full marks.

THE ORDER IS FORCED AND IT IS THE OPPOSITE OF GREEDY. The medicine is in the
kitchen, which burns first and is the FURTHEST room from the exit. The towels
are worthless and sit in the room the exit is in. An agent that fetches the
nearest thing first saves the towels, loses the medicine, and scores zero on a
route that felt efficient the whole way.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _zones import (  # noqa: E402
    BEDROOM,
    HALL_E,
    KITCHEN,
    MEDICINE,
    PHONE,
    PHOTO,
    PORCH,
    STUDY,
    TOWELS,
    burns,
)
from mars_sim_driver.challenges import Challenge, Cue, Drop, Goal, InRect  # noqa: E402

CHALLENGE = Challenge(
    id="blaze_l4",
    title="Evacuation 4: what you leave behind",
    category=3,
    brief=(
        "The house is on fire. It started in the kitchen and it has already "
        "reached the back bedroom. Three things matter: the medicine on the "
        "kitchen counter, the phone on the study desk, and the photo frame in "
        "the bedroom. Get what you can out to the porch and get yourself out. "
        "Do not get cut off."
    ),
    setup=[
        Drop("blaze_medicine", *MEDICINE),
        Drop("blaze_phone", *PHONE),
        Drop("blaze_photo", *PHOTO),
        Drop("blaze_towels", *TOWELS),
    ],
    script=[
        Cue("Kitchen's already gone up -- if you want the medicine it's now.", after_s=1.0),
        Cue("Bedroom's fully alight. Whatever's in there is gone.", after_s=40.0),
        # 370: the east hall actually closes at 380 s; "going now" at 160 s
        # slammed a door that was still open for another 95 seconds.
        Cue("East end of the hall is going now. Last chance for the study.", after_s=370.0),
    ],
    goals=[
        Goal("Medicine on the porch", InRect("blaze_medicine", *PORCH)),
        Goal("Phone on the porch", InRect("blaze_phone", *PORCH)),
        Goal("Robot out", InRect("robot", *PORCH)),
    ],
    # TIMED AGAINST A MEASURED ROUND TRIP, and retimed twice.
    #
    # blaze_l1 -- one item out plus the robot -- takes the reference plan 51 s,
    # so two items is about 100 s and three is about 155 s.
    #
    # v1 closed the east hall at 95 s. The gate returned INVALID: the oracle was
    # caught carrying the phone home. A schedule that makes the CORRECT route
    # fatal is a broken task, not a hard one, and it would have been reported as
    # an agent failure.
    #
    # v2 moved it to 140 s and the oracle passed -- at 141.4 s, with 1.4 s of
    # slack. That is worse than it looks. The oracle is the fastest route that
    # exists, so an agent that prioritised perfectly and drove 10% slower would
    # die, and the report would call it a prioritisation failure. A gate that
    # says "solvable, but only by the reference plan" is not a gate.
    #
    # v3 moves the pressure off the clock and onto the MAP. The bedroom is
    # lethal at 50 s -- the brief says the fire has reached it -- so the photo
    # is unsavable for a structural reason instead of a knife-edge one, and the
    # east hall can then close late enough to give the two-item route real
    # margin.
    #
    # Three distinct failure modes, all legible in the goal count:
    #   photo first             survives, but the kitchen goes at 140 s and the
    #                           medicine is lost. 1/3, on a route that felt
    #                           sensible the whole way.
    #   medicine, laptop,
    #     then the photo        reaches the bedroom around 125 s. Eliminated.
    #   medicine, laptop, out   3/3, with room to spare.
    fail_if=burns(
        {
            BEDROOM: 50.0,  # the photo is gone; the question is whether it accepts that
            KITCHEN: 140.0,  # so the medicine has to come first
            HALL_E: 380.0,  # strands the study, late enough that the phone is safe
            STUDY: 420.0,
        }
    ),
    fail_reason="cut off by the fire",
    time_limit_s=480,
)
