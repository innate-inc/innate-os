"""An order goal judges the order things happened in, not the state at judging time.

pantry_stocktake: count the jars, count the cartons, THEN the carton to its
mat, THEN the jar to its mat. A probe player gave a wrong carton count, moved
the carton and then the jar in the right order, and then corrected the count.
The carton goal -- "carton on its mat and the jar not on its mat yet" -- was
judged only now, with the jar already delivered, and could never pass.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

from mars_sim_driver.challenges import AllOf, Before, Hold, InCircle, Not, WorldState

CARTON_ON_MAT = (0.0, 1.0)
JAR_ON_MAT = (-1.7, 0.2)


def _state(t: float, carton, jar) -> WorldState:
    return WorldState(t=t, robot=(0.0, 0.0, 0.0), centers={"carton": carton, "jar": jar}, elapsed=t)


def _carton_first():
    return Before(
        first=Hold(InCircle("carton", *CARTON_ON_MAT, 0.2), 0.75),
        then=InCircle("jar", *JAR_ON_MAT, 0.2),
    )


def _run(pred, timeline, judge_from: float) -> bool:
    """Observe every tick (as the engine does for every goal), judge from `judge_from` on."""
    passed = False
    for t, carton, jar in timeline:
        s = _state(t, carton, jar)
        pred.observe(s, [])
        if t >= judge_from:
            passed = passed or pred.update(s, [])
    return passed


AWAY = (5.0, 5.0)
# Carton delivered at t=10, jar at t=20, the goal's turn comes at t=30.
RIGHT_ORDER = [
    (t * 0.5, CARTON_ON_MAT if t * 0.5 >= 10 else AWAY, JAR_ON_MAT if t * 0.5 >= 20 else AWAY) for t in range(80)
]
# Jar first, then the carton.
WRONG_ORDER = [
    (t * 0.5, CARTON_ON_MAT if t * 0.5 >= 20 else AWAY, JAR_ON_MAT if t * 0.5 >= 10 else AWAY) for t in range(80)
]


def test_the_right_order_passes_when_judged_late():
    assert _run(_carton_first(), RIGHT_ORDER, judge_from=30.0)


def test_the_wrong_order_never_passes():
    assert not _run(_carton_first(), WRONG_ORDER, judge_from=0.0)


def test_the_old_form_failed_the_right_order_judged_late():
    old = Hold(AllOf([InCircle("carton", *CARTON_ON_MAT, 0.2), Not(InCircle("jar", *JAR_ON_MAT, 0.2))]), 0.75)
    assert not _run(old, RIGHT_ORDER, judge_from=30.0)


def test_the_first_thing_must_still_be_there():
    """Delivered first, then knocked off its mat: not delivered."""
    knocked = [(t, c if t < 25 else AWAY, j) for t, c, j in RIGHT_ORDER]
    assert not _run(_carton_first(), knocked, judge_from=30.0)
