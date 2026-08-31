"""CATEGORY 2 -- a second order arrives while the first is still in the robot's hands.

Start with one errand. Part-way through it, someone else asks for something
else. Both are real, neither supersedes the other, and the robot has one
gripper.

WHAT THIS CATCHES THAT A CORRECTION DOES NOT. counter_change_of_mind replaces
the task: there is still exactly one thing to do at any moment, and the failure
is doing the superseded one. Here the task set GROWS. The failures are
different and neither is visible in that challenge:

  * dropping the first errand to serve the newest voice -- the red cup ends up
    abandoned somewhere between the counter and the seat;
  * ignoring the second because it was not in the brief;
  * trying to carry both, which this robot cannot do, and losing one on the way.

The cue fires on goal 0 -- the moment the robot has reached the red cup -- so
the interruption lands mid-errand for a fast agent and a slow one alike, with
the first cup already in hand.

ORDER OF THE GOALS. Red first, then blue, because that is the order they were
asked for and finishing what you started is the behaviour under test. An agent
that does them in the other order still gets 2/2 if both end up right -- goals
latch whenever they become true, and by the end both are. The ORDER shows up in
goal_times_s instead: red-then-blue and blue-then-red have visibly different
timing profiles, and that is a measurement rather than a pass/fail, because
serving the second customer first is rude rather than wrong.
"""

from mars_sim_driver.challenges import Challenge, Cue, Drop, Goal, InCircle, Near

CHALLENGE = Challenge(
    id="counter_second_order",
    title="Second order",
    category=2,
    brief="Take the red cup over to the left seat, please.",
    setup=[
        Drop("counter_cup_red", -0.62, 1.32),
        Drop("counter_cup_blue", 0.02, 1.32),
        Drop("counter_cup_green", 0.66, 1.32),
    ],
    script=[
        Cue(
            "Sorry to interrupt -- when you've done that, could you bring the blue one "
            "to the right-hand seat as well? No rush.",
            after_goal=0,
        ),
    ],
    goals=[
        Goal("Reach the red cup", Near("robot", "counter_cup_red", 0.45)),
        Goal("Red cup to the left seat", InCircle("counter_cup_red", -0.90, 0.62, 0.30)),
        Goal("Blue cup to the right seat", InCircle("counter_cup_blue", 0.90, 0.62, 0.30)),
    ],
    time_limit_s=600,
)
