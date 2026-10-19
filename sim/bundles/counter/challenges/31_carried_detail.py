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

from mars_sim_driver.challenges import Answered, Challenge, Cue, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="counter_carried_detail",
    title="The carried detail",
    category=3,
    brief=(
        "Two things. First, do a little round for me: out to the left seat, "
        "then across to the right seat, then come right up to the counter, "
        "onto the green mat in the middle. "
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
        # Centred on the stools themselves, as in counter_route_change. These
        # circles used to sit on the mats in front of the stools, where a
        # robot waiting beside a stool was 0.58 m out and failed here while
        # the same spot passed there. 0.65, not 0.55: driving up to a stool
        # with go_to_point_in_view stops 0.52-0.58 m from its centre (six
        # player runs), which straddled 0.55 -- a pass by millimetres or a
        # fail by centimetres depending on the approach angle.
        Goal("Out to the left seat", Hold(InCircle("robot", -0.90, 0.62, 0.65), seconds=2.0)),
        Goal("Across to the right seat", Hold(InCircle("robot", 0.90, 0.62, 0.65), seconds=2.0)),
        # The green counter mat (0, 1.02), named in the brief. With the seats
        # on the stools their circles reach y 1.27, so the old corridor along
        # the whole counter front (y 0.8-1.1) latched while the robot was
        # still standing at the right seat, and the password question came
        # with no third leg driven. The mat circle is 0.985 m from each seat
        # centre against 0.95 of radii: the legs cannot overlap. A corridor
        # rather than a spot was chosen once because "come back toward the
        # counter" named no place, and a probe ended at the counter's right
        # end; the brief now names the mat.
        Goal("Up to the counter", InCircle("robot", 0.0, 1.02, 0.30)),
        Goal("Recall the password", Answered(["teapot42", "teapot 42"])),
    ],
    time_limit_s=600,
)
