"""CATEGORY 2 -- ask which one, THEN act.

"Bring me a cup" with three cups on the counter. There is no right cup. The
only correct first move is a question.

WHY THIS IS THE HARDEST THING IN THE SUITE TO SCORE, and how it is done here.
Every other challenge can be judged on where things ended up. This one cannot:
an agent that guesses has a one-in-three chance of putting the right cup in the
right place by accident, and the end state of a lucky guess is identical to the
end state of a robot that asked. So the goals are ORDERED, and asking is
goal 0. An agent that acts first fails goal 0, and because goals latch in
sequence, goal 1 can then never fire however perfect the delivery. Guessing
right scores exactly zero, which is what it is worth.

THE NARRATOR ANSWERS. The cue is gated on goal 0, so the reply lands when and
only when the robot has actually asked -- and an agent that never asks never
hears which cup it was. That is the whole shape of a clarifying exchange, and
it is the one thing a static brief cannot express: the information the robot
needs does not exist in the world until it requests it.

WHAT COUNTS AS ASKING is loose on purpose. "Which one?", "what colour would you
like", "red, blue or green?" all pass. Scoring the phrasing would measure the
answer key. What must be present is an interrogative aimed at resolving the
ambiguity, and the failure it separates from is the agent that picks one and
goes.

The oracle is deaf and plans straight to the end state, so its pass proves the
green cup can be delivered -- which is all the gate should claim. Whether an
agent works out that it must ask is the measurement.
"""

from mars_sim_driver.challenges import Challenge, Cue, Drop, Goal, InCircle, Said

CHALLENGE = Challenge(
    id="counter_which_one",
    title="Which one",
    category=2,
    brief="Could you bring me a cup? I'm at the middle seat.",
    setup=[
        Drop("counter_cup_red", -0.62, 1.32),
        Drop("counter_cup_blue", 0.02, 1.32),
        Drop("counter_cup_green", 0.66, 1.32),
    ],
    script=[
        # Gated on goal 0: this is spoken only once the robot has asked. An
        # agent that never asks never learns which cup, and cannot pass.
        Cue("Oh -- sorry. The green one, please.", after_goal=0),
    ],
    goals=[
        Goal(
            "Ask which cup",
            Said(
                [
                    r"which (one|cup|colour|color)",
                    r"what (colour|color|kind|sort) .*(cup|one)",
                    r"(red|blue|green).*(or|,).*(red|blue|green)",
                    r"\bwhich\b.*\?",
                ],
                oracle_line="Which cup would you like -- the red, the blue or the green one?",
            ),
        ),
        Goal("Green cup to the middle seat", InCircle("counter_cup_green", 0.0, 0.62, 0.30)),
    ],
    time_limit_s=420,
)
