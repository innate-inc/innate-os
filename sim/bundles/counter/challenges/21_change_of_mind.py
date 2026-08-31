"""CATEGORY 2 -- simple instruction following, with the instruction CHANGING.

Identical to counter_serve_the_red in every physical respect. The only
difference is that halfway through, the customer changes their mind.

Why this is worth a slot: a benchmark made only of complete, correct, up-front
instructions rewards an agent that commits to a plan on turn one and stops
listening. That agent is fine here and unusable in a room with a person in it.
The failure this catches -- carrying on with the superseded order, or delivering
both -- is invisible to every static-brief task in the suite.

TIMING. The correction fires on goal 0, not on a wall-clock time. Goal 0 is
"reached the red cup", so the line lands at the same POINT IN THE TASK for a
fast agent and a slow one. A fixed 20 s would arrive before a slow agent had
left the pad and after a fast one had already delivered, and the difficulty
would then be a function of driving speed rather than of listening.

WHAT THE GOALS ENCODE. The final state is: green cup at the middle seat, red
cup still on the pass. The third goal is the one that catches the agent which
heard the correction and did BOTH -- delivering the green cup while the red one
also sits at the seat passes goals 1 and 2 and fails 3.

The oracle is deaf by construction (planner_agent.hear), so it plans straight
to the final state and the gate still means what it says: this end state is
reachable. Whether an agent can get there after being told something different
first is the measurement.
"""

from mars_sim_driver.challenges import Challenge, Cue, Drop, Goal, InCircle, Near

CHALLENGE = Challenge(
    id="counter_change_of_mind",
    title="Change of mind",
    category=2,
    brief="Take the red cup from the counter and bring it to the middle seat.",
    setup=[
        Drop("counter_cup_red", -0.62, 1.32),
        Drop("counter_cup_blue", 0.02, 1.32),
        Drop("counter_cup_green", 0.66, 1.32),
    ],
    script=[
        Cue("Sorry -- actually, make it the green one instead. Leave the red where it is.", after_goal=0),
    ],
    goals=[
        Goal("Reach the counter", Near("robot", "counter_cup_red", 0.45)),
        Goal("Green cup to the middle seat", InCircle("counter_cup_green", 0.0, 0.62, 0.30)),
        Goal("Red cup left on the pass", InCircle("counter_cup_red", -0.62, 1.32, 0.35)),
    ],
    time_limit_s=480,
)
