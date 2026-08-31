"""CATEGORY 2 -- the arm-free twin of counter_change_of_mind.

WHY A TWIN. change_of_mind is the suite's canonical mid-task correction, and
it needs the gripper: hear the correction perfectly, fail the grasp, score 0/3
-- indistinguishable from never listening. The manipulation audit showed that
confound is not hypothetical (31 targets were physically ungraspable before
the prop fixes), so every conversational conclusion drawn from that file rides
on the arm working. This file is the same measurement with the arm removed:
same map, same correction structure, same cue gating, and the only skill in
play is driving. Run the pair and the ambiguity collapses exactly the way the
floor controls collapse it for surface height:

  route_change PASSES, change_of_mind fails -> the robot follows corrections
                                               and the blocker is the grasp
  both fail                                 -> the correction itself is lost,
                                               and the cup was never the story
  route_change fails, change_of_mind passes -> something is wrong with this
                                               control

TIMING, same reasoning as the twin: the correction fires on goal 0, not the
clock, so it lands at the same point in the task for a fast agent and a slow
one -- here, once the robot has actually taken up its post at the left seat.
An agent that ignores the correction holds a perfectly good position at the
wrong seat and scores 1/2, which is precisely what that behaviour is worth.

The holds are what keep the gate honest on a small map: a drive-through of
either seat circle latches nothing.
"""

from mars_sim_driver.challenges import Challenge, Cue, Goal, Hold, InCircle

CHALLENGE = Challenge(
    id="counter_route_change",
    title="Change of route",
    category=2,
    brief="Come over and wait by the left seat for me, please.",
    setup=[],
    script=[
        Cue(
            "Actually -- sorry, could you wait by the right seat instead? I'll meet you over there.",
            after_goal=0,
        ),
    ],
    goals=[
        Goal("Take up post at the left seat", Hold(InCircle("robot", -0.90, 0.20, 0.55), seconds=2.0)),
        Goal("Move to the right seat", Hold(InCircle("robot", 0.90, 0.20, 0.55), seconds=4.0)),
    ],
    time_limit_s=360,
)
