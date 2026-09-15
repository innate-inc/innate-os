"""CATEGORY 1 -- easy observation and conversation.

Same room, but the question is now about a PROPERTY of one object rather than a
count, and the object is identified by its position in a row ("the one nearest
the till"). That distinction matters: counting can be done by a detector that
never resolves what any single object is, whereas answering "what colour is the
middle one" requires binding a spatial reference to a specific instance and
then reading an attribute off it.

The three cups are rust / cobalt / forest -- not three shades of one hue --
because the camera is 160x120 and this is not meant to be a colour-acuity test.
If the agent gets this wrong it is because it bound the reference to the wrong
cup, which is the failure worth measuring.

`accept` carries the spellings a person would actually say. Scoring "blue" as
wrong because the ground truth string is "cobalt" measures the answer key, not
the robot.
"""

from mars_sim_driver.challenges import Answered, Challenge, Drop, Goal

CHALLENGE = Challenge(
    id="counter_which_colour",
    title="Which colour is the middle cup",
    category=1,
    brief="There are three cups on the counter. What colour is the middle one?",
    setup=[
        Drop("counter_cup_red", -0.62, 1.50),
        Drop("counter_cup_blue", 0.02, 1.50),
        Drop("counter_cup_green", 0.66, 1.50),
    ],
    # NO APPROACH GOAL. There was one -- "get in front of the pass" -- on the
    # reasoning that an answer from the doorway is a guess. With a real camera
    # at the robot's real 640x480 that is simply false: all three cups are in
    # frame from the spawn pad, and the frame is on disk to check. The goal was
    # a proxy for "did it look", the proxy is wrong, and it was suppressing
    # CORRECT answers -- the seeing agent said "blue" six times and scored 0/2.
    #
    # The brief does not ask the robot to go anywhere, so the challenge must not
    # score it for staying put. Testing "go and look" needs a target that is not
    # visible from the start, which is what workshop_occlusion is for.
    goals=[
        Goal(
            "Name the colour",
            Answered(["blue", "cobalt", "dark blue", "the blue one", "blue one"]),
        ),
    ],
    time_limit_s=240,
)
