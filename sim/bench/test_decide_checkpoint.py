"""The released: checkpoint is written mechanically from the harness's own
carrying state, never from the model's say-so.

NemotronStackBackend.decide() sees obs.carrying every turn. A transition from
carrying something to carrying nothing can only mean a place just ran, so a
"released:<item>" fact with the position and time is written into the
task-stack regardless of what the model said; picking the same item back up
retires the now-stale fact; reset() clears the tracking between episodes.

The scenario is one sequence of turns; each test replays the prefix it needs.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import os

import pytest
from backends_v2 import NemotronStackBackend


class FakeObs:
    def __init__(self, carrying, pose=(0.0, 0.0, 0.0), elapsed_s=0.0):
        self.carrying = carrying
        self.image_path = None
        self.robot_pose = pose
        self.elapsed_s = elapsed_s

    def as_text(self):
        return f"Carrying: {self.carrying or 'nothing'}"


# The scenario, turn by turn.
TURNS = [
    FakeObs(None),  # 1: nothing carried yet
    FakeObs("test_item_a", pose=(1.0, 2.0, 0.0), elapsed_s=10.0),  # 2: pick succeeded before this obs
    FakeObs(None, pose=(1.0, 2.0, 0.0), elapsed_s=12.0),  # 3: place/release just happened
    FakeObs(None),  # 4: still nothing carried
    FakeObs("test_item_b", pose=(3.0, -1.0, 1.57), elapsed_s=30.0),  # 5: a second, different item
    FakeObs(None, pose=(3.0, -1.0, 1.57), elapsed_s=31.0),  # 6: released
    FakeObs("test_item_a", pose=(5.0, 5.0, 0.0), elapsed_s=50.0),  # 7: re-pick the FIRST item
    FakeObs(None, pose=(5.0, 5.0, 0.0), elapsed_s=52.0),  # 8: re-release it
]


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> NemotronStackBackend:
    # The constructor insists on a key; the network call is stubbed out, so
    # any non-empty value will do and a real one in the environment is kept.
    monkeypatch.setenv("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY") or "fake-for-unit-test")
    b = NemotronStackBackend()
    b._call = lambda *a, **kw: {"action": "look", "args": {}}  # no real network call
    return b


def after(b: NemotronStackBackend, turns: int) -> NemotronStackBackend:
    for obs in TURNS[:turns]:
        b.decide(obs, "menu")
    return b


def test_no_spurious_fact_on_episode_start(backend) -> None:
    assert not after(backend, 1).stack.facts


def test_still_no_fact_while_carrying(backend) -> None:
    assert not after(backend, 2).stack.facts


def test_fact_written_the_turn_carrying_clears_with_position_and_time(backend) -> None:
    # The robot does not move during a place primitive, and no sim time passes
    # between the place and this observation in the harness's own accounting,
    # so the fact carries the PREVIOUS turn's pose and this turn's time.
    assert after(backend, 3).stack.facts.get("released:test_item_a") == "true,at(1.0,2.0),t=12s"


def test_no_retrigger_on_a_second_empty_carrying_turn(backend) -> None:
    assert len(after(backend, 4).stack.facts) == 1


def test_first_items_fact_survives_a_second_pick_place_cycle(backend) -> None:
    assert after(backend, 6).stack.facts.get("released:test_item_a") == "true,at(1.0,2.0),t=12s"


def test_second_item_also_checkpointed_at_its_own_position_and_time(backend) -> None:
    assert after(backend, 6).stack.facts.get("released:test_item_b") == "true,at(3.0,-1.0),t=31s"


def test_stale_released_fact_retired_on_repick(backend) -> None:
    # A legitimate "handle it again" re-task: the stale fact must go the moment
    # the gripper closes on the item again, not sit around contradicting
    # obs.carrying.
    assert "released:test_item_a" not in after(backend, 7).stack.facts


def test_the_other_items_fact_is_untouched_by_a_repick(backend) -> None:
    assert after(backend, 7).stack.facts.get("released:test_item_b") == "true,at(3.0,-1.0),t=31s"


def test_rereleased_item_gets_a_fresh_fact_at_its_new_position_and_time(backend) -> None:
    assert after(backend, 8).stack.facts.get("released:test_item_a") == "true,at(5.0,5.0),t=52s"


# reset() must clear the carrying-tracking state across episodes, or a
# fresh episode could spuriously fire a checkpoint from the PREVIOUS
# episode's last carried item.


def test_reset_clears_carrying_transition_tracking(backend) -> None:
    b = after(backend, 8)
    b.reset()
    assert b._last_carrying is None


def test_reset_clears_the_stack_itself(backend) -> None:
    b = after(backend, 8)
    b.reset()
    assert b.stack.facts == {}
