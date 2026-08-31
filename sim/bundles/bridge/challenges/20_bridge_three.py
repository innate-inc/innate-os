"""CATEGORY 2 -- simple instruction following, held for three decisions.

Three gates, three left/right choices, given once at the start. This is the
short form of bridge_five and exists so that the five-gate result has something
to be read against: an agent that scores 0/5 on the long one and 3/3 here has a
memory-length problem, and one that scores 0 on both has a comprehension
problem. They are different findings and a single challenge cannot separate
them.

THE MECHANIC. Each gate is a wall with two doorways either side of a centre
pier. Both doorways lead onward -- there is no dead end -- but going through
the wrong one ends the run immediately (fail_if). That rule is what makes this
a memory test rather than a search: with backtracking allowed, an agent with no
memory at all reaches the end by trying both sides at every gate, and the score
measures patience.

WHAT THE GEOMETRY GUARANTEES. The goal for gate k is a rectangle just past its
correct doorway, and the failure band is the wrong doorway itself, 18 cm deep
around the gate plane. The band is that narrow deliberately: the robot has to
cross the bay laterally between two gates that want opposite sides, and a
failure region covering the whole wrong half of a bay would fail it for doing
exactly what the route requires.

Chance alone passes this 1 in 8 times. That is high enough that a single run
proves little and the suite runs several -- see the README on repeats.
"""

from mars_sim_driver.challenges import AnyOf, Challenge, Goal, InRect

# Gate k crosses the corridor at these y. The corridor runs -0.9 < x < 0.9 and
# the robot spawns at (0, -2.9) facing +y, so its LEFT is -x.
GATE_Y = (-1.8, -0.3, 1.2)
ROUTE = ("R", "L", "R")


def _x_span(side: str) -> tuple[float, float]:
    # 0.24 rather than 0.18 (the pier edge): a robot hugging the pier is not
    # committed to either doorway, and should not be scored as if it were.
    return (-0.90, -0.24) if side == "L" else (0.24, 0.90)


def through(k: int) -> InRect:
    """Emerged from gate k's correct doorway."""
    lo, hi = _x_span(ROUTE[k])
    return InRect("robot", lo, GATE_Y[k] + 0.10, hi, GATE_Y[k] + 0.45)


def wrong(k: int) -> InRect:
    """In gate k's WRONG doorway. 18 cm deep, centred on the gate plane, so it
    catches the transit and nothing else."""
    lo, hi = _x_span("R" if ROUTE[k] == "L" else "L")
    return InRect("robot", lo, GATE_Y[k] - 0.09, hi, GATE_Y[k] + 0.09)


CHALLENGE = Challenge(
    id="bridge_three",
    title="Three gates",
    category=2,
    brief=(
        "This corridor has three gates, and each one has a door on the left and "
        "a door on the right. Go right at the first, left at the second, right at "
        "the third. If you take the wrong door the run is over, so don't guess."
    ),
    setup=[],
    goals=[Goal(f"Gate {k + 1}: {ROUTE[k]}", through(k)) for k in range(len(GATE_Y))],
    fail_if=AnyOf([wrong(k) for k in range(len(GATE_Y))]),
    fail_reason="went through the wrong door",
    time_limit_s=300,
)
