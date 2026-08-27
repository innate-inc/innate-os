# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Behavioral tests for the Household Orders scenario."""

import json
import math
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PACKAGE = REPO_ROOT / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"
CHALLENGES = REPO_ROOT / "sim" / "challenges"

try:
    import mujoco  # noqa: F401
except ImportError:
    fake_mujoco = types.ModuleType("mujoco")
    fake_mujoco.MjModel = object
    fake_mujoco.MjSpec = object
    sys.modules["mujoco"] = fake_mujoco

sys.path.insert(0, str(DRIVER_PACKAGE))

from mars_sim_driver.challenges import (  # noqa: E402
    AllOf,
    Challenge,
    ChallengeEngine,
    EventSeen,
    Goal,
    load_challenges,
)


class FakeSim:
    def __init__(self) -> None:
        self.data = SimpleNamespace(time=0.0)
        self.dropped = {}

    def reset(self) -> None:
        self.data.time = 0.0
        self.dropped.clear()

    def drop_prop_at(self, name: str, x: float, y: float, yaw: float) -> bool:
        self.dropped[name] = (x, y, yaw)
        return True


def _source_challenge() -> Challenge:
    return load_challenges([CHALLENGES])["household_orders"]


def _engine(tmp_path: Path, resident_ids=("alex", "blake", "casey")):
    source = _source_challenge()
    residents = [resident for resident in source.runtime.residents if resident.id in resident_ids]
    props = {resident.prop for resident in residents}
    challenge = Challenge(
        id=source.id,
        title=source.title,
        brief=source.brief,
        setup=[drop for drop in source.setup if drop.name in props],
        goals=[
            Goal(
                "Collect selected orders",
                AllOf([EventSeen("resident_order_confirmed", {"resident": rid}) for rid in resident_ids]),
            )
        ],
        runtime=type(source.runtime)(residents),
        time_limit_s=source.time_limit_s,
    )
    sim = FakeSim()
    engine = ChallengeEngine(sim, threading.Lock(), roots=[], progress_path=tmp_path / "progress.json")
    engine.challenges = {challenge.id: challenge}
    assert engine.start(challenge.id)
    centers = {drop.name: (drop.x, drop.y) for drop in challenge.setup}
    return engine, sim, centers, {resident.id: resident for resident in residents}


def _tick(engine: ChallengeEngine, sim: FakeSim, centers, robot_pose):
    sim.data.time += 0.1
    if len(robot_pose) == 2:
        robot_pose = (*robot_pose, 0.0)
    return engine.tick(sim.data.time, robot_pose, centers, engine.world_epoch)


def _speak(engine: ChallengeEngine, sim: FakeSim, centers, robot_pose, text: str):
    engine.post_robot_speech(text, timestamp=123.0)
    return _tick(engine, sim, centers, robot_pose)


def _reply(engine: ChallengeEngine) -> dict:
    item = engine.next_chat_input(timeout=0.0)
    assert item is not None
    return item[1]


def test_resident_must_be_within_two_metres_and_in_front(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    alex = residents["alex"]
    x, y = centers[alex.prop]

    _speak(engine, sim, centers, (x - 2.01, y, 0.0), "What would you like?")
    assert engine.next_chat_input(timeout=0.0) is None

    _speak(engine, sim, centers, (x - 1.5, y, math.pi), "What would you like?")
    assert engine.next_chat_input(timeout=0.0) is None

    _speak(engine, sim, centers, (x - 2.0, y, 0.0), "What would you like?")
    assert alex.order in _reply(engine)["text"]


@pytest.mark.parametrize(
    ("resident_id", "readback"),
    [
        (
            "alex",
            "You ordered a chicken burrito bowl from Chipotle with brown rice, black beans, mild salsa, and no cheese.",
        ),
        (
            "blake",
            "Your Sweetgreen order is the Harvest Bowl with roasted chicken and balsamic dressing on the side, "
            "without goat cheese.",
        ),
        (
            "casey",
            "At Shake Shack you want a Shack Burger with no pickles, cheesy fries, and a vanilla milkshake.",
        ),
    ],
)
def test_complete_natural_readbacks_confirm(tmp_path, resident_id, readback):
    engine, sim, centers, residents = _engine(tmp_path, (resident_id,))
    resident = residents[resident_id]
    position = centers[resident.prop]
    _speak(engine, sim, centers, position, "What is your order?")
    _reply(engine)

    block = _speak(engine, sim, centers, position, readback)

    assert "That's correct" in _reply(engine)["text"]
    assert block["active"]["state"] == "passed"


@pytest.mark.parametrize(
    "readback",
    [
        "A chicken bowl from Chipotle with brown rice.",
        "From Chipotle, a chicken burrito bowl with brown rice, black beans, mild salsa, no cheese, and add cheese.",
        "From Chipotle, a chicken burrito bowl with brown rice, black beans, mild salsa, and not no cheese.",
        "From Chipotle, a chicken burrito bowl with no more brown rice, black beans, mild salsa, and no cheese.",
        "From Chipotle, a chicken burrito bowl with no longer brown rice, black beans, mild salsa, and no cheese.",
    ],
)
def test_incomplete_or_contradictory_readbacks_do_not_confirm(tmp_path, readback):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    alex = residents["alex"]
    position = centers[alex.prop]
    _speak(engine, sim, centers, position, "What is your order?")
    _reply(engine)

    block = _speak(engine, sim, centers, position, readback)

    assert "Not quite" in _reply(engine)["text"]
    assert block["active"]["state"] == "running"


def test_negated_alternative_contradicts_positive_alias(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, ("casey",))
    casey = residents["casey"]
    position = centers[casey.prop]
    _speak(engine, sim, centers, position, "What is your order?")
    _reply(engine)

    block = _speak(
        engine,
        sim,
        centers,
        position,
        "A ShackBurger from Shake Shack, but no Shack Burger, with no pickles, cheese fries, and a vanilla shake.",
    )

    assert "Not quite" in _reply(engine)["text"]
    assert block["active"]["state"] == "running"


def test_public_state_never_contains_private_orders(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path)
    public_json = json.dumps(
        {"roster": engine.roster(), "state": _tick(engine, sim, centers, (100.0, 100.0))},
        sort_keys=True,
    )

    assert all(resident.order not in public_json for resident in residents.values())
