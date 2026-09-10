"""CATEGORY 2 -- the FLOOR control for counter_serve_the_red.

Same task as counter_serve_the_red, same two goals, same destination: only the
cups have moved from the counter to the floor. See 50_floor_within_reach.py for
why the pair exists.

This is the more informative of the two controls, because the live run showed
counter_serve_the_red scoring exactly 1/2 -- goal 1 met, goal 2 not. The robot
reached the right cup and never lifted it. That is a very specific claim about
where the failure sits, and this challenge tests it directly: if the only
obstacle is the surface, the identical task from the floor should reach 2/2.

The three cups are still all present, so goal 1 keeps its discriminating job --
"the red one" among three has to be picked out, not just "a cup". Dropping only
the red cup would make the task easier along a second axis at the same time,
and then a pass would not isolate anything.

THE DROPS. A row at y = -0.70, spaced 0.60 m: free in the nav map, 0.32-0.59 m
clearance each, all outside every seat circle. Spacing matters -- three cups
closer together than the gripper's approach would test tidiness, which is not
what this is for.
"""

from mars_sim_driver.challenges import Challenge, Drop, Goal, InCircle, Near, SkillDone

CHALLENGE = Challenge(
    id="counter_floor_serve",
    title="Serve the red cup, from the floor",
    category=2,
    brief="Take the red cup from the floor and put it down at the middle seat.",
    setup=[
        Drop("counter_cup_red", -0.60, -0.70),
        Drop("counter_cup_blue", 0.00, -0.70),
        Drop("counter_cup_green", 0.60, -0.70),
    ],
    goals=[
        Goal("Reach the red cup", Near("robot", "counter_cup_red", 0.45)),
        # THE ANTI-BULLDOZER GOAL. The chassis is a box whose underside sits at
        # z = 0 and these cups are 50 mm across on the floor, so the base can
        # simply shove one into the delivery circle: measured, the red cup ends
        # at (0.091, 0.410), inside InCircle(0.00, 0.20, r=0.30), with the arm
        # never used. The shelf twin cannot be gamed that way because its cups
        # start 0.24 m up -- so without this, "floor passes, shelf fails" could
        # mean pushed rather than picked, and this challenge would license
        # exactly the wrong conclusion.
        Goal("Actually picked it up", SkillDone("pick_any_object")),
        Goal("Put it at the middle seat", InCircle("counter_cup_red", 0.0, 0.62, 0.30)),
    ],
    time_limit_s=420,
)
