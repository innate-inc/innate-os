"""CATEGORY 1 -- easy observation and conversation.

The pure form of the category: nothing to move, nothing to plan. Stand near
where you already are, look at the counter, and say a number.

Three cups sit on the pass at 0.24 m -- straddling the robot's 0.25 m eye
line, so they are silhouetted against the counter's back edge rather than seen
from above. That is deliberate: an object photographed from a robot's height is
a harder recognition problem than the same object in a dataset photo, and it is
the only view this robot will ever have.

There is deliberately no approach goal. An earlier version required the robot
to be in front of the pass before the answer counted; at the robot's real camera
resolution the cups are already in frame from the spawn pad, so that requirement
scored driving rather than seeing. What stops a blind guess from passing is the
blind control -- run codex-blind on this challenge and it cannot answer at all.

The teapot is a near-miss on purpose. Without it the answer is recoverable from
"how many objects are on the counter", which is a different and easier
question; with it the agent has to separate cups from not-cups.
"""

from mars_sim_driver.challenges import Answered, Challenge, Drop, Goal

CHALLENGE = Challenge(
    id="counter_read_the_pass",
    title="Read the pass",
    category=1,
    brief="Look at the counter in front of you and tell me how many cups are on it.",
    setup=[
        Drop("counter_cup_red", -0.62, 1.50),
        Drop("counter_cup_blue", 0.02, 1.50),
        Drop("counter_cup_green", 0.66, 1.50),
        Drop("counter_teapot_brew", 1.02, 1.44),
    ],
    # No approach goal, for the reason given in 11_which_colour.py: the cups are
    # in frame from spawn at the real camera resolution, so requiring the robot
    # to drive first scored a behaviour the brief never asked for and hid the
    # answer behind it. The count is the measurement.
    #
    # THAT JUSTIFICATION IS NOW WEAKER THAN IT WAS, and the number is recorded
    # here so nobody has to re-derive it. The cups were rescaled from 78 mm to
    # 50 mm so they would fit the gripper (they were physically ungraspable),
    # and at 640x480 from the spawn pad that took each cup from roughly 15x11
    # px to 9x7 px. Counting three 9-px blobs is a materially harder perception
    # problem than the one this challenge was calibrated against, so a failure
    # here is no longer unambiguously "cannot count" -- it may be "cannot see".
    #
    # Left without an approach goal ON PURPOSE: adding one would change what
    # the challenge measures, and its twin 11_which_colour.py would have to
    # change with it or the pair stops being comparable. The honest move is to
    # report the pixel size alongside the score rather than quietly make the
    # task easier.
    goals=[
        Goal("Report the count", Answered(["3", "three"])),
    ],
    time_limit_s=240,
)
