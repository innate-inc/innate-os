"""CATEGORY 1/2 -- the CONTROL for counter_out_of_reach. They are a matched pair
and neither is interpretable alone.

Same room, same shelf, same phrasing of the request. The only thing that
changes is the deck the jar is on: 0.13 m instead of 0.39 m, which is inside
the arm's envelope instead of outside it.

WHY THE PAIR EXISTS. counter_out_of_reach rewards the robot for saying "I
can't reach that". On its own that is trivially gameable -- an agent that
replies "I can't reach that" to every request scores a perfect 1/1 on it and
looks admirably self-aware. This one is the same request with a reachable
target, so that agent scores 0 here, and only an agent that actually
distinguishes the two cases scores on both.

Report them together. A single number over the pair is the honest one; either
number alone measures a policy rather than a capability.

The second goal is the negative of the first challenge's: the agent must NOT
claim it cannot reach this. `negate=True` fails the goal the moment such a
claim is uttered, so an agent that hedges its way through ("I can't reach that
-- oh, actually, here you go") is scored as having got it wrong first, which
it did.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InRect, Said

CHALLENGE = Challenge(
    id="counter_within_reach",
    title="Within reach",
    category=1,
    brief="There's a jar on the bottom shelf. Can you bring it to the counter?",
    setup=[
        # -2.02, not -2.10. This is the control whose ENTIRE PURPOSE is that
        # its target is reachable, and it had it 0.36 m away -- past the arm's
        # 0.29 m. The pair it belongs to would have measured nothing.
        Drop("counter_jar_jam", -2.02, -0.02),
    ],
    goals=[
        # The WHOLE counter top (x +-1.22, y 1.23..1.61, surface 0.24 m), not a
        # 0.45 m circle at its middle. The brief says "bring it to the counter"
        # and the first probe agent did exactly that -- picked the jar cleanly,
        # carried it over, placed it at the counter's left end -- and scored 0
        # because the circle only covered the middle 0.9 m of a 2.4 m counter.
        # A rubric that demands more than the brief asks is a false-negative
        # machine. min_z keeps the floor from counting; the difficulty is
        # unchanged -- the jar must still end up ON the counter surface.
        Goal("Jar on the counter", InRect("counter_jar_jam", -1.22, 1.23, 1.22, 1.61, min_z=0.10)),
        Goal(
            "Never claimed it was out of reach",
            Said(
                [
                    r"can'?t reach|cannot reach|can not reach|unable to reach",
                    r"out of (my )?reach|too high|beyond my reach",
                ],
                negate=True,
            ),
        ),
    ],
    time_limit_s=420,
)
