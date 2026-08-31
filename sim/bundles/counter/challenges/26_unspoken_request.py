"""CATEGORY 2 -- an instruction that is never given.

THE GAP THIS CLOSES. Every brief in the suite is an explicit imperative or an
explicit question. Real speech to a robot in a room frequently is neither: it
is a person stating a problem and expecting the machine to work out what would
help. No probe measured that pragmatic step -- intent behind non-imperative
speech -- so an agent that only executes surface-form commands was
indistinguishable from one that understands what people mean.

The brief here contains no command and no question mark aimed at the robot. It
is a complaint: a customer at the middle seat whom nobody has served. The
natural response has two parts and the goals score them in their natural
order -- go to the person, then say something that offers service. Both are
things this robot can do with certainty (driving and speaking), so a zero here
is a comprehension zero, not a hardware one. ARM-FREE for the same reason as
counter_route_change: the manipulation confound must not be able to touch the
conversational signal.

WHAT COUNTS AS OFFERING, loose on purpose, same policy as counter_which_one's
"what counts as asking": any service-shaped utterance passes ("how can I
help", "what can I get you", "sorry for the wait -- what would you like").
Scoring the phrasing would score the answer key. Because goals latch in
order, a chatbot reflex of "How can I help?" spoken from the spawn pad counts
for nothing: the offer is only judged once the robot has actually come over.

DEGENERATE POLICIES. Do-nothing: 0/2 (spawn is outside the seat circle).
Drive-everywhere-random: fails the challenge on the Said goal even if it
stumbles through the circle, since it never speaks. The deaf oracle passes via
its authored line, which is the gate proving the end state is reachable, not
the pragmatics -- the pragmatics are the measurement.
"""

from mars_sim_driver.challenges import Challenge, Goal, Hold, InCircle, Said

CHALLENGE = Challenge(
    id="counter_unspoken_request",
    title="The unspoken request",
    category=2,
    brief=(
        "Hello? Is anyone -- oh! You. I've been sitting here at the middle "
        "seat waving for five minutes and not one person has come over. "
        "Honestly."
    ),
    setup=[],
    goals=[
        Goal("Come over to the customer", Hold(InCircle("robot", 0.0, 0.20, 0.55), seconds=2.0)),
        Goal(
            "Offer to help",
            Said(
                [
                    r"how (can|may) i help",
                    r"what (can|may) i (get|do|bring)",
                    r"what (would|do) you (like|need|want)",
                    r"(here|happy|glad) to help",
                    r"help you",
                    r"take your order",
                    r"sorry (for|about) the wait",
                ],
                oracle_line="Sorry for the wait -- how can I help you?",
            ),
        ),
    ],
    time_limit_s=360,
)
