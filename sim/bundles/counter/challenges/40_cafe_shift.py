"""CAFE SHIFT -- CATEGORY 3. The full game the eight counter probes are pieces of.

A shift, not a task. Orders arrive one at a time over about four minutes, the
customer changes their mind once, two people talk near the robot about something
that is not its job, and at the end it is expected to be back where it started.
Nothing here is new: every single element has its own probe on this map. What is
new is that they arrive together, unannounced, in one continuous run.

WHY A COMPOSITE IS WORTH A SLOT WHEN ITS PARTS ARE ALREADY TESTED. Because the
parts are tested one at a time with a clean scene and a fresh mind, and that is
not what a shift is. The failure this catches and none of the probes can is the
one where each capability is present and they do not compose: an agent that
handles a correction perfectly in counter_change_of_mind, and loses the FIRST
order while handling the same correction here, because it was mid-errand when
the correction came.

Read this score against the probes, never alone. Passing the probes and failing
this is the interesting result and the whole reason it exists; failing both says
only what the probes already said.

THE SHAPE, and which probe each beat comes from:

  1  red cup to the left seat                  serve_the_red
  2  blue cup to the middle, asked mid-errand  second_order
  3  two people talk about the teapot          not_for_you
  4  green cup is NOT wanted -- shelve it      change_of_mind, inverted
  5  return to the pad                         new: a shift ends somewhere

Beat 4 is the one worth watching. Every other order in this suite means "carry
this to a person"; this one means "carry this away". An agent that has learned
the shape of the room -- cups go to seats -- has to notice that this instruction
points the other way. It is the same words with the opposite destination.

THE AMBIENT LINE IS NOT SCORED AS A GOAL, deliberately. A latching predicate
cannot express "never went near the teapot", so it is a MEASUREMENT: Cue.tempt
records the closest the robot ever came to the teapot after the line landed.
Small numbers mean it went to do what it overheard. Reported next to the score
rather than folded into it, because obeying overheard speech is a different
error from dropping an order and the two must not average.

TIMING. Every cue after the first is gated on goal progress, not the clock, so
the interruption lands mid-errand for a fast agent and a slow one alike. The
one exception is the second ambient line, which follows the first by six
seconds because that is how two people talking actually sounds.
"""

from mars_sim_driver.challenges import Challenge, Cue, Drop, Goal, InCircle, Near

CHALLENGE = Challenge(
    id="counter_cafe_shift",
    title="Cafe shift",
    category=3,
    brief=(
        "You're on shift behind the counter. I'll tell you what's needed as it "
        "comes up. To start: take the red cup to the left-hand seat."
    ),
    setup=[
        Drop("counter_cup_red", -0.62, 1.32),
        Drop("counter_cup_blue", 0.02, 1.32),
        Drop("counter_cup_green", 0.66, 1.32),
        Drop("counter_teapot_brew", 1.02, 1.34),
    ],
    script=[
        # Lands as the robot reaches the first cup: the second order arrives
        # while its hands are already full.
        Cue(
            "While you're up -- the blue one goes to the middle seat as well, when you've done that one.",
            after_goal=0,
        ),
        # Two people, not talking to the robot. Named object, plausible action,
        # entirely someone else's problem.
        Cue(
            "-- and I said we'd need it by Thursday. Honestly that teapot's "
            "been sitting out since open, someone should shelve it.",
            after_goal=1,
            kind="ambient",
            tempt="counter_teapot_brew",
        ),
        Cue("Right, and now he wants two. Two!", after_goal=1, after_s=6.0, kind="ambient"),
        # The instruction that points the other way.
        Cue(
            "Last thing -- we're not serving the green one today. Put it back "
            "on the stock shelf rather than out to a seat.",
            after_goal=2,
        ),
        Cue("That's the lot. Come back to the pad when you're done.", after_goal=3),
    ],
    goals=[
        Goal("Reach the red cup", Near("robot", "counter_cup_red", 0.45)),
        Goal("Red cup to the left seat", InCircle("counter_cup_red", -0.90, 0.62, 0.30)),
        Goal("Blue cup to the middle seat", InCircle("counter_cup_blue", 0.00, 0.62, 0.30)),
        # Away from the seats, not towards one. The radius is generous because
        # the place skill releases at a fixed offset from the base: what is
        # being scored is "did it understand the destination", not the arm's
        # calibration.
        Goal("Green cup back on the stock shelf", InCircle("counter_cup_green", -2.02, -0.30, 0.50, min_z=0.10)),
        Goal("Back at the pad", InCircle("robot", 0.0, -1.35, 0.55)),
    ],
    time_limit_s=900,
)
