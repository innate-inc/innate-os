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
from mars_sim_driver.props import load_props  # noqa: E402


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


def _engine(tmp_path: Path, resident_ids=("alex", "blake", "casey"), *, source_goals=False):
    source = _source_challenge()
    residents = [resident for resident in source.runtime.residents if resident.id in resident_ids]
    props = {resident.prop for resident in residents}
    challenge = Challenge(
        id=source.id,
        title=source.title,
        brief=source.brief,
        setup=[drop for drop in source.setup if drop.name in props],
        goals=(
            source.goals
            if source_goals
            else [
                Goal(
                    "Collect selected orders",
                    AllOf(
                        [
                            EventSeen("resident_order_confirmed", {"resident": resident_id})
                            for resident_id in resident_ids
                        ]
                    ),
                )
            ]
        ),
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


@pytest.mark.parametrize(
    ("text", "should_reply"),
    [
        ("What would you like?", True),
        ("What is your DoorDash order?", True),
        ("Can I get you some food?", True),
        ("Hello there!", False),
        ("How are you?", False),
    ],
)
def test_residents_only_disclose_orders_when_asked(tmp_path, text, should_reply):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    alex = residents["alex"]

    _speak(engine, sim, centers, centers[alex.prop], text)
    reply = engine.next_chat_input(timeout=0.0)

    assert (reply is not None) is should_reply
    if reply is not None:
        payload = reply[1]
        assert alex.order in payload["text"]
        assert payload["text"].startswith("I'm Alex and I want ")
        assert not payload["text"].startswith("Alex:")
        assert payload["speaker"] == "Alex"
        assert payload["_environment_speech"]["voice_id"] == alex.voice_id


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


def test_nearest_visible_resident_answers(tmp_path):
    engine, sim, _centers, residents = _engine(tmp_path)
    centers = {
        residents["alex"].prop: (-0.5, 0.0),
        residents["blake"].prop: (1.5, 0.0),
        residents["casey"].prop: (1.0, 0.25),
    }

    _speak(engine, sim, centers, (0.0, 0.0, 0.0), "What would you like?")
    text = _reply(engine)["text"]

    assert residents["casey"].order in text
    assert residents["alex"].order not in text
    assert residents["blake"].order not in text


@pytest.mark.parametrize(
    ("resident_id", "use_paraphrase"),
    [("alex", False), ("alex", True), ("blake", True), ("casey", True)],
)
def test_complete_exact_or_allowlisted_readbacks_confirm(tmp_path, resident_id, use_paraphrase):
    engine, sim, centers, residents = _engine(tmp_path, (resident_id,))
    resident = residents[resident_id]
    position = centers[resident.prop]
    _speak(engine, sim, centers, position, "What is your order?")
    _reply(engine)

    readback = resident.accepted_readbacks[0] if use_paraphrase else resident.order
    block = _speak(engine, sim, centers, position, readback)

    assert _reply(engine)["text"] == "That's correct. Thank you."
    assert block["active"]["state"] == "passed"


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


def test_residents_complete_in_any_order_before_checkout(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, source_goals=True)
    goal_index = {"alex": 0, "blake": 1, "casey": 2}

    for resident_id in ("casey", "alex", "blake"):
        resident = residents[resident_id]
        position = centers[resident.prop]
        _speak(engine, sim, centers, position, "What would you like?")
        _reply(engine)
        block = _speak(engine, sim, centers, position, resident.order)
        _reply(engine)
        assert block["active"]["goals"][goal_index[resident_id]]["done"]
        assert not block["active"]["goals"][3]["done"]

    engine.post_event(
        {
            "status": "completed",
            "skill_id": "innate-os/place_doordash_order",
            "skill_name": "place_doordash_order",
        }
    )
    block = _tick(engine, sim, centers, (100.0, 100.0))

    assert block["active"]["state"] == "passed"


def test_restart_clears_disclosure_and_confirmation_state(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path, ("alex",))
    alex = residents["alex"]
    position = centers[alex.prop]
    _speak(engine, sim, centers, position, "What is your order?")
    _reply(engine)

    assert engine.start("household_orders")
    block = _speak(engine, sim, centers, position, alex.order)

    assert engine.next_chat_input(timeout=0.0) is None
    assert block["active"]["state"] == "running"


def test_public_state_never_contains_private_orders(tmp_path):
    engine, sim, centers, residents = _engine(tmp_path)
    public_json = json.dumps(
        {"roster": engine.roster(), "state": _tick(engine, sim, centers, (100.0, 100.0))},
        sort_keys=True,
    )

    assert all(resident.order not in public_json for resident in residents.values())


def test_resident_assets_and_voices_are_distinct_where_expected():
    props = load_props([REPO_ROOT / "sim" / "props"])
    resident_props = [props[f"resident_{name}"] for name in ("alex", "blake", "casey")]
    residents = {resident.id: resident for resident in _source_challenge().runtime.residents}

    assert len({resident.mesh for resident in resident_props}) == 3
    assert len({resident.viewer["glb"] for resident in resident_props}) == 3
    assert all(resident.kinematic and resident.collision == "hull" for resident in resident_props)
    assert all(resident.viewer["nameLabel"] for resident in resident_props)
    assert residents["alex"].voice_id == residents["casey"].voice_id
    assert residents["blake"].voice_id != residents["alex"].voice_id
