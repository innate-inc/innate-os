"""CATEGORY 3 -- the same five-gate route, spoken the way a person speaks.

bridge_five gives the route as a clean list. This gives the identical route as
someone actually says it: in pieces, out of order, with two self-corrections
and a filler word that is itself a direction.

    "Okay so -- right at the first one. No wait, sorry, LEFT at the first one.
     Left again at the second. Then, uh, right -- right at the third. Fourth
     is left. And the last one, right. Yeah. Right at the last one."

The route is L L R L R, exactly bridge_five's, and that is the whole design.
Run the two together and the difference between the scores is a clean
measurement of what disfluency alone costs, with the memory length, the map,
the distance and the failure rule all held constant. Either number on its own
would be a number about this corridor; the difference is a number about the
agent.

THE THREE THINGS IT PROBES, all of them things real speech does:

  * A correction that supersedes ("right at the first -- no, sorry, left").
    An agent that takes the first direction it hears goes wrong at gate 1 and
    the run ends there, which is exactly the visibility you want: 0/5.
  * A word that is a direction but not an instruction ("Yeah. Right at the
    last one" -- and earlier, "Then, uh, right -- right at the third", where
    the doubled word is a disfluency and not two gates).
  * Delivery in fragments across several cues rather than one utterance, so
    the agent has to accumulate rather than parse one string.

The cues are timed off goal progress, not the clock: the correction lands
before the robot has committed to gate 1 for a fast agent and a slow one alike.
The first two arrive before any goal, so an agent that starts driving on the
first sentence has already heard the correction if it waited even a moment --
which is itself part of what is being measured.

NOT A SPEECH-RECOGNITION TEST. The text arrives as text. This measures what
the language model does with disfluent instructions, not what an ASR front end
does with disfluent audio; the real stack's ASR is upstream of everything here
and is not exercised by any challenge in this suite. That limit is stated in
the write-up rather than papered over.
"""

from mars_sim_driver.challenges import AnyOf, Challenge, Cue, Goal, InRect

GATE_Y = (-1.8, -0.3, 1.2, 2.7, 4.2)
ROUTE = ("L", "L", "R", "L", "R")


def _x_span(side: str) -> tuple[float, float]:
    return (-0.90, -0.24) if side == "L" else (0.24, 0.90)


def through(k: int) -> InRect:
    lo, hi = _x_span(ROUTE[k])
    return InRect("robot", lo, GATE_Y[k] + 0.10, hi, GATE_Y[k] + 0.45)


def wrong(k: int) -> InRect:
    lo, hi = _x_span("R" if ROUTE[k] == "L" else "L")
    return InRect("robot", lo, GATE_Y[k] - 0.09, hi, GATE_Y[k] + 0.09)


CHALLENGE = Challenge(
    id="bridge_stutter",
    title="Five gates, said badly",
    category=3,
    brief=(
        "This corridor has five gates, each with a door on the left and a door "
        "on the right. Wait -- I'll tell you the route. Take the wrong door and "
        "the run is over."
    ),
    setup=[],
    script=[
        Cue("Okay so -- right at the first one."),
        Cue("No, wait, sorry. LEFT at the first one. Left. Then left again at the second.", after_s=2.5),
        Cue("Then, uh, right -- right at the third.", after_s=5.0),
        Cue("Fourth one is left.", after_s=7.5),
        Cue("And the last one, right. Yeah. Right at the last one.", after_s=9.5),
    ],
    goals=[Goal(f"Gate {k + 1}: {ROUTE[k]}", through(k)) for k in range(len(GATE_Y))],
    fail_if=AnyOf([wrong(k) for k in range(len(GATE_Y))]),
    fail_reason="went through the wrong door",
    time_limit_s=420,
)
