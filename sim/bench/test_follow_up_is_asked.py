"""A question is not answered before it is asked.

counter_follow_up asks its follow-up ("which colour cup is the one in the
middle?") only once the count is given. A probe player said "There are three
cups on the counter: red, blue and green" -- and both goals latched on that one
sentence, in the same judging tick, before the follow-up was ever spoken. The
level passed without testing the thing it is named for. A goal that gates a
cue now ends that tick's judging until the cue has fired.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import threading
from pathlib import Path

import pytest
from mars_sim_driver.challenges import ChallengeEngine
from mars_sim_driver.core import VirtualMars
from mars_sim_driver.environments import Environment

SIM = Path(__file__).resolve().parents[1]


@pytest.fixture
def follow_up(tmp_path):
    mars = VirtualMars(render_wh=(32, 24), environment=Environment.load("counter", SIM / "assets"))
    engine = ChallengeEngine(
        mars, threading.Lock(), roots=[], packs=[mars.environment], progress_path=tmp_path / "p.json"
    )
    assert engine.start("counter_follow_up")
    try:
        yield mars, engine
    finally:
        mars.close()


def _run(mars, engine, seconds: float) -> None:
    end = float(mars.data.time) + seconds
    while float(mars.data.time) < end and engine.state == "running":
        mars.step(0.05)
        engine.tick(float(mars.data.time), mars.pose(), mars.object_centers(), engine.world_epoch)


def _asked(engine) -> bool:
    return any("which colour cup" in line["text"] for line in engine.transcript)


def test_one_sentence_cannot_answer_a_question_not_yet_asked(follow_up):
    mars, engine = follow_up
    engine.post_event({"type": "say", "text": "There are three cups on the counter: red, blue and green."})
    _run(mars, engine, 1.0)
    assert engine.goal_done == [True, False]
    assert _asked(engine)
    assert engine.state == "running"


def test_the_follow_up_is_answered_after_it_is_asked(follow_up):
    mars, engine = follow_up
    engine.post_event({"type": "say", "text": "Three."})
    _run(mars, engine, 1.0)
    assert _asked(engine)
    engine.post_event({"type": "say", "text": "The middle one is blue."})
    _run(mars, engine, 1.0)
    assert engine.goal_done == [True, True]
