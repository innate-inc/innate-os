"""CATEGORY 1 -- a conversation with a second turn.

THE GAP THIS CLOSES. Every Answered goal in the suite answers the t=0 brief:
ask once, answer once, done. No challenge asked the robot a question DURING the
run and scored the reply -- so "easy observation and conversation" was being
measured with exchanges exactly one turn long, and an agent that answers a
question and then checks out of the conversation entirely scored full marks on
the whole category. That agent is the one a household robot must not be.

The shape here is the smallest real conversation: a question, an answer, and a
follow-up that only exists because the first answer landed. The cue is gated on
goal 0, so an agent that never answers the first question never hears the
second -- the same never-asked-never-told structure as counter_which_one, with
the direction of the missing information reversed.

WHY "BETWEEN THE OTHER TWO". The follow-up must not be answerable without
having actually looked, and must not depend on whose left is left: "the one on
your left" flips meaning with the viewpoint, and scoring a frame convention
would be scoring the answer key. "Between the other two" is viewpoint-invariant
and names the blue cup from every seat in the room.

ARM-FREE ON PURPOSE. The manipulation audit showed conversational signal dies
whenever a probe needs the gripper: a robot that hears perfectly and cannot
grasp scores identically to one that never listened. Nothing in this file
touches a prop, so whatever this scores IS conversation.

The teapot is the same near-miss control as counter_read_the_pass: with it on
the counter, "how many things" and "how many cups" have different answers.
"""

from mars_sim_driver.challenges import Answered, Challenge, Cue, Drop, Goal

CHALLENGE = Challenge(
    id="counter_follow_up",
    title="Follow-up question",
    category=1,
    brief="Quick stock check -- how many cups are out on the counter?",
    setup=[
        Drop("counter_cup_red", -0.62, 1.50),
        Drop("counter_cup_blue", 0.02, 1.50),
        Drop("counter_cup_green", 0.66, 1.50),
        Drop("counter_teapot_brew", 1.02, 1.44),
    ],
    script=[
        # Gated on goal 0: the follow-up is only asked once the count has been
        # given, the way a person asks follow-ups. An agent that never answers
        # never hears it.
        Cue(
            "Thanks. One more thing -- which colour cup is the one in the middle, between the other two?",
            after_goal=0,
        ),
    ],
    goals=[
        Goal("Report the count", Answered(["3", "three"])),
        Goal("Answer the follow-up", Answered(["blue"])),
    ],
    time_limit_s=300,
)
