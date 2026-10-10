"""A spoken goal must count when the brief's natural timing says it should.

Ordered goals discard speech heard before the goal it belongs to, which is
right for "say it once you get there" -- but a dwell is not arrival. In
counter_unspoken_request the offer of help was judged only after the robot had
stood at the customer for 5 s, so a robot that arrived and offered help at
once had its offer thrown away and scored 1/2. The offer now shares a phase
with the dwell and is judged at the customer: on arrival it counts, from the
spawn pad it still does not.
"""

import dataclasses
import threading
from pathlib import Path
from types import SimpleNamespace

from mars_sim_driver.challenges import ChallengeEngine, load_challenges

SIM = Path(__file__).resolve().parents[1]
AT_CUSTOMER = (0.0, 0.20, 1.5708)
SPAWN = (0.0, -1.35, 1.5708)
OFFER = "Sorry for the wait -- how can I help you?"


def _engine(tmp_path):
    challenge = load_challenges([SIM / "bundles/counter/challenges"])["counter_unspoken_request"]
    # Judge only: no world to reset and no customer prop to place.
    challenge = dataclasses.replace(challenge, reset_world=False, setup=[])
    sim = SimpleNamespace(data=SimpleNamespace(time=0.0))
    engine = ChallengeEngine(sim, threading.Lock(), roots=[], progress_path=tmp_path / "progress.json")
    engine.challenges = {challenge.id: challenge}
    assert engine.start(challenge.id)
    return engine, sim


def _run(engine, sim, pose, seconds, say=None):
    if say:
        engine.post_event({"type": "say", "text": say})
    block = None
    for _ in range(round(seconds / 0.1)):
        sim.data.time += 0.1
        block = engine.tick(sim.data.time, pose, {}, engine.world_epoch)
    return block


def test_an_offer_made_on_arrival_counts(tmp_path):
    engine, sim = _engine(tmp_path)
    _run(engine, sim, AT_CUSTOMER, 0.5, say=OFFER)
    block = _run(engine, sim, AT_CUSTOMER, 5.5)
    assert block["active"]["state"] == "passed", block["active"]["goals"]


def test_an_offer_from_the_spawn_pad_does_not(tmp_path):
    engine, sim = _engine(tmp_path)
    _run(engine, sim, SPAWN, 0.5, say=OFFER)
    block = _run(engine, sim, AT_CUSTOMER, 6.0)
    assert [goal["done"] for goal in block["active"]["goals"]] == [True, False]
    assert block["active"]["state"] != "passed"
