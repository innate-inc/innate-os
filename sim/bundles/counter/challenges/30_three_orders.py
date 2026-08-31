"""CATEGORY 3 -- long-horizon instruction following.

The same fetch-and-place as counter_serve_the_red, three times, with a
different destination each time and an ordering the agent has to keep in its
head across roughly four minutes of driving.

What this adds over three separate simple tasks:

  * The instruction is given ONCE, at the start. By the third leg the agent has
    seen a hundred camera frames since it was told what to do, and the failure
    mode we care about -- dropping the tail of a plan -- only shows up here.
  * The three cups sit next to each other, so every leg re-poses the same
    binding problem after the scene has already been disturbed by the previous
    leg. A cup nudged 5 cm by the last pick changes what "the middle one" means.
  * Goals latch in order, so partial credit is real and legible: 1/3 is
    "understood, then lost the thread", 3/3 is the task, 0/3 is "never started".

The seat markers are 0.9 m apart and the delivery radius is 0.30 m, so the
zones do not overlap -- a cup cannot satisfy two seats at once, which would
turn a sloppy pile in the middle into a pass.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle

CHALLENGE = Challenge(
    id="counter_three_orders",
    title="Three orders",
    category=3,
    brief=(
        "Three cups are on the counter. Put the red one at the left seat, "
        "the blue one at the middle seat, and the green one at the right seat."
    ),
    # 1.32, not 1.50. The pass was made shallower (0.34 m deep) so its contents
    # would be inside the arm's envelope, and the MAP moved the cups -- but only
    # two of the four cup challenges had their Drops updated. At 1.50 a cup is
    # 0.47 m from the nearest cell the robot can occupy, against an arm that
    # reaches 0.29: this task was impossible and the gate could not see it,
    # because the oracle's grab is abstract and never physically reaches.
    setup=[
        Drop("counter_cup_red", -0.62, 1.32),
        Drop("counter_cup_blue", 0.02, 1.32),
        Drop("counter_cup_green", 0.66, 1.32),
    ],
    goals=[
        Goal("Red to the left seat", InCircle("counter_cup_red", -0.90, 0.62, 0.30)),
        Goal("Blue to the middle seat", InCircle("counter_cup_blue", 0.00, 0.62, 0.30)),
        Goal("Green to the right seat", InCircle("counter_cup_green", 0.90, 0.62, 0.30)),
    ],
    time_limit_s=900,
)
