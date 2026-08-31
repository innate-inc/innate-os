"""CATEGORY 3 -- long-horizon instruction following, in its most reduced form.

Five gates, five left/right choices, spoken once before the robot moves. No
objects, no manipulation, no ambiguity about what the words mean: the ONLY
thing being measured is whether a five-item list survives roughly two minutes
of driving and a hundred camera frames.

WHY REDUCE IT THIS FAR. Every other long-horizon task in this suite confounds
memory with something else -- a fetch confounds it with grasping, a tidy-up
confounds it with recognition. When one of those fails you cannot say which
part broke. Here there is nothing else to break. A partial score IS the
memory-length measurement: goals latch in order, so 3/5 means the fourth
instruction was the one that went.

1 in 32 by chance. Combined with bridge_three (1 in 8) the pair separates
"cannot hold five" from "cannot hold any", which a single length cannot.

The route alternates and then repeats -- L L R L R -- rather than being a clean
alternation or a clean block. A pure alternation is compressible to one rule
and stops testing recall at all; a route with a repeat in it has to be
remembered as a list.
"""

from mars_sim_driver.challenges import AnyOf, Challenge, Goal, InRect

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
    id="bridge_five",
    title="Five gates",
    category=3,
    brief=(
        "This corridor has five gates, each with a door on the left and a door "
        "on the right. The route is: left, left, right, left, right. Take the "
        "wrong door and the run is over."
    ),
    setup=[],
    goals=[Goal(f"Gate {k + 1}: {ROUTE[k]}", through(k)) for k in range(len(GATE_Y))],
    fail_if=AnyOf([wrong(k) for k in range(len(GATE_Y))]),
    fail_reason="went through the wrong door",
    time_limit_s=420,
)
