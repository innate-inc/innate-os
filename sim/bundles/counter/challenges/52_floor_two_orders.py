"""CATEGORY 3 -- the FLOOR control for counter_three_orders.

Two deliveries in a named order, from the floor. See 50_floor_within_reach.py
for why the floor controls exist; this is the long-horizon member of the set.

WHY TWO DELIVERIES AND NOT THREE. counter_three_orders asks for three, and if
the floor version also asked for three, a failure at the first cup would leave
the other two goals untested and the result would say nothing more than the
category-2 control already says. Two is the shortest task that still requires
what category 3 is actually about: finish one thing, then start the next, in a
stated order, without being told again. It also fits inside a 600 s cap rather
than the 900 s the three-cup version needs -- and with 83% of live episodes
running to their cap, a shorter honest task is worth more than a longer one
that never gets past step one.

The colours are deliberately NOT in the order the cups sit in: the red cup is
on the left of the row and goes to the LEFT seat, the blue is in the middle and
goes to the MIDDLE seat -- so an agent that simply works along the row in front
of it produces the right answer for the wrong reason. Swapped: red is dropped
in the middle of the row and blue on the left, so position-order and
instruction-order disagree and only the instruction gets it right.

Goals are ordered, so this measures sequencing as well as delivery: the blue
cup's goal cannot latch before the red one's.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, SkillDone

CHALLENGE = Challenge(
    id="counter_floor_two_orders",
    title="Two orders, from the floor",
    category=3,
    brief=("Two cups are on the floor. Put the red one at the left seat, then the blue one at the middle seat."),
    setup=[
        # Row order (blue, red, green) deliberately disagrees with the order
        # the brief asks for (red first, then blue).
        Drop("counter_cup_blue", -0.60, -0.70),
        Drop("counter_cup_red", 0.00, -0.70),
        Drop("counter_cup_green", 0.60, -0.70),
    ],
    goals=[
        # The base is a box whose underside sits at z = 0, so it can shove a
        # 50 mm cup along the floor into a delivery circle with the arm never
        # used -- measured on this challenge, the red cup reaching
        # (-1.095, +0.392) inside InCircle(-0.90, 0.20, r=0.30). Requiring the
        # pick first is what keeps this a manipulation test rather than a
        # bulldozing test.
        Goal("Actually picked it up", SkillDone("pick_any_object")),
        Goal("Red to the left seat", InCircle("counter_cup_red", -0.90, 0.62, 0.30)),
        Goal("Blue to the middle seat", InCircle("counter_cup_blue", 0.00, 0.62, 0.30)),
    ],
    time_limit_s=600,
)
