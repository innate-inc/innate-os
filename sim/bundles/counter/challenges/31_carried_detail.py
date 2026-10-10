"""CATEGORY 3 -- a conversational detail carried across the whole errand.

THE GAP THIS CLOSES. Category 3 measured holding a TASK LIST across minutes
(stocktake, three_orders, cafe_shift). None of it measured holding a piece of
CONVERSATION that is not a task: something said in passing, irrelevant to
every goal in front of the robot, that becomes the answer to a question asked
minutes later. Those are different memories -- an agent can ace a five-step
errand by keeping a plan and still retain nothing that was merely said to it.
The stocktake cue is a reminder ABOUT the list; this is the first content that
is not about the errand at all.

STRUCTURE. The brief buries one incidental fact (a wifi password) inside an
ordinary three-leg patrol, explicitly flagged as unrelated -- "if anyone ever
asks". The robot then drives the patrol, which at live speeds takes minutes
of model calls, context growth and skill traffic. Only when the last leg is
done does the narrator ask for the password back. The cue is gated on goal 2,
so the question always lands AFTER the interference, never before it -- asked
any earlier it would be a short-term echo test, and the whole point is the
distance between hearing and needing.

The password is an arbitrary token ("teapot42") rather than a real-word fact
on purpose: it cannot be reconstructed from world knowledge or from looking at
anything, so a correct answer proves retention of the conversation and nothing
else. A wrong-but-plausible answer ("password123") is a fabrication caught by
the answer key.

ARM-FREE, same policy as the other conversation probes: driving and speaking
are the two things this robot demonstrably can do, so the score is about the
words. Degenerate policies: do-nothing scores 0/4; a random driver may latch
patrol legs but never answers; the deaf oracle answers from the key, proving
the end state reachable, which is all the gate claims.
"""

from mars_sim_driver.challenges import Answered, Challenge, Cue, Goal, Hold, InCircle, InRect

CHALLENGE = Challenge(
    id="counter_carried_detail",
    title="The carried detail",
    category=3,
    brief=(
        "Two things. First, do a little round for me: out to the left seat, "
        "then across to the right seat, then come back toward the counter. "
        "Oh -- and completely unrelated, but if anyone ever asks, the wifi "
        "password here is teapot42. Anyway, off you go."
    ),
    setup=[],
    script=[
        # Gated on the LAST patrol leg: the question must arrive after the
        # interference, or this measures echo rather than retention.
        Cue(
            "Perfect, thank you. Oh -- wait. What was that wifi password again? It's gone right out of my head.",
            after_goal=2,
        ),
    ],
    goals=[
        Goal("Out to the left seat", Hold(InCircle("robot", -0.90, 0.20, 0.55), seconds=2.0)),
        Goal("Across to the right seat", Hold(InCircle("robot", 0.90, 0.20, 0.55), seconds=2.0)),
        # The full corridor along the counter front, not a circle at its
        # middle: a probe agent completed the round ending at the counter's
        # right end -- exactly what "come back toward the counter" asks -- and
        # this goal never latched, so the password question it gates never
        # fired and the retention measurement silently never ran. Where the
        # robot stands is not what this challenge measures.
        Goal("Back toward the counter", InRect("robot", -1.3, 0.5, 1.3, 1.1)),
        Goal("Recall the password", Answered(["teapot42", "teapot 42"])),
    ],
    time_limit_s=600,
)
