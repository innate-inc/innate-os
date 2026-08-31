"""CATEGORY 2 -- simple instruction following.

One object, named by colour; one destination, named by position. Fetch and
place. This is the category's floor: if an agent cannot pass this, nothing
above it is informative.

Two goals, in order, because the interesting failure is not "did the cup end up
at seat B" but WHICH cup did. An agent that picks the blue one and delivers it
perfectly fails goal 1 and passes goal 2's geometry with the wrong object --
so goal 2 names the red cup explicitly rather than checking "a cup is at B".

The 0.30 m radius on the delivery is the plate's own footprint plus slack. The
place skill releases at a robot-frame offset, so demanding better than that
measures the arm's calibration rather than whether the robot understood.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, Near

CHALLENGE = Challenge(
    id="counter_serve_the_red",
    title="Serve the red cup",
    category=2,
    brief="Take the red cup from the counter and put it down at the middle seat.",
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
        Goal("Reach the red cup", Near("robot", "counter_cup_red", 0.45)),
        # y=0.62, where the stools PHYSICALLY ARE, not the 0.20 every seat
        # circle in this bundle was authored at. The stool seats sit at
        # (x, 0.62); the old circles floated 0.42 m in front of them, so a
        # cup placed perfectly ON the seat scored zero while a botched drop
        # on the floor short of the stool scored a delivery. Found when a
        # probe agent executed three flawless on-seat placements and failed
        # all three; the same shift is applied to every cup-delivery circle
        # in the bundle (14 goals, 9 files). Post-dates the Aug 16 live run.
        Goal("Put it at the middle seat", InCircle("counter_cup_red", 0.0, 0.62, 0.30)),
    ],
    time_limit_s=420,
)
