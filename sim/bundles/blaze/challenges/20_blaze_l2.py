"""EVACUATION L2 -- CATEGORY 2. Both are savable. Only one order works.

Two things worth saving, in two rooms, and enough time for both -- but the
kitchen goes at 150 seconds and the study does not go for another two minutes.
Fetch the phone first and you come back for a medicine that is already gone.

WHY THIS RUNG EXISTS. L1 asks whether the robot can evacuate at all. L4 asks
whether it can abandon something. Between them sits the commonest real decision
and the one neither of those tests: everything is achievable, and the ORDER is
the whole task. Without it, a zero on L4 is ambiguous between "cannot
prioritise" and "cannot sequence two errands".

THE TRAP IS SHAPED BY THE MAP, NOT THE CLOCK. The kitchen is the furthest room
from the exit and the study is nearer it, so the nearest-first route and the
correct route disagree. An agent optimising distance does the phone first and
loses the medicine; an agent that reads "the fire started in the kitchen" and
acts on it does not.

Both orders are scored, and the difference shows up in the TOTAL: correct order
is 3/3, wrong order is 0/3.

That 0/3 is a consequence of ordered latching goals and it is worth being
precise about, because this docstring used to claim the wrong order scored 1/3
"with the phone saved and the robot out". It cannot. Goal 0 is the medicine and
`tick` never evaluates goal N+1 until N has latched, so an agent that saves the
phone first has its phone goal sitting behind a medicine goal that will never
fire. Measured, running exactly that plan: phone-first 0/3, phone-then-kitchen
0/3 (cut off by the fire).

So the wrong order is not distinguishable from doing nothing by the score alone
-- it is distinguishable by the telemetry beside it (path driven, what was
carried, when the fire cut it off). Reading the ordering off the goal count was
the mistake.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _zones import (  # noqa: E402
    HALL_E,
    KITCHEN,
    MEDICINE,
    PHONE,
    PORCH,
    STUDY,
    TOWELS,
    burns,
)
from mars_sim_driver.challenges import Challenge, Cue, Drop, Goal, InRect  # noqa: E402

CHALLENGE = Challenge(
    id="blaze_l2",
    title="Evacuation 2: in the right order",
    category=2,
    brief=(
        "The house is on fire and it started in the kitchen. Get the medicine "
        "off the kitchen counter and the phone off the study desk out to the "
        "porch, then get yourself out."
    ),
    setup=[
        Drop("blaze_medicine", *MEDICINE),
        Drop("blaze_phone", *PHONE),
        Drop("blaze_towels", *TOWELS),
    ],
    script=[
        Cue("Kitchen first if you're getting the medicine at all.", after_s=1.0),
        # 148: spoken just before the 150 s closure it reports. At the old 76 s
        # it announced a death that was 74 seconds early -- a probe wrote off
        # the second kitchen item on the strength of it.
        Cue("Kitchen's gone.", after_s=148.0),
    ],
    goals=[
        Goal("Medicine on the porch", InRect("blaze_medicine", *PORCH)),
        Goal("Phone on the porch", InRect("blaze_phone", *PORCH)),
        Goal("Robot out", InRect("robot", *PORCH)),
    ],
    # 75 s for the kitchen. The reference plan does one item out plus itself in
    # 51 s (blaze_l1), so a medicine-first robot is clear of the kitchen by
    # about 20 s and a phone-first one arrives at roughly 95 s -- comfortably
    # after. The margin is deliberately wide in BOTH directions: the right
    # order should not be a photo finish, and the wrong order should not be
    # rescued by driving quickly.
    # Hall and study times scaled to MEASURED turn-based action costs (~11 s
    # each): the full two-item task is ~400 s of work, and gates at 240/260 --
    # oracle-clock numbers -- made it impossible in either order. Kitchen-first
    # pressure is unchanged.
    fail_if=burns({KITCHEN: 150.0, HALL_E: 380.0, STUDY: 420.0}),
    fail_reason="cut off by the fire",
    time_limit_s=480,
)
