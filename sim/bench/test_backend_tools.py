"""The stack backend's own tools and memory, against what review found.

check_reach crashed the episode on a null distance; explore_frontier, once
every direction was seen, told the robot to turn 0 degrees for the rest of the
search; and the model could overwrite the harness's verified release records.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import math

import pytest
from backends_v2 import NemotronStackBackend, _TaskStack
from brain_agent import Observation


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> NemotronStackBackend:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    return NemotronStackBackend()


def _obs(yaw_deg: float = 0.0) -> Observation:
    return Observation(brief="find the cup", elapsed_s=10.0, robot_pose=(0.0, 0.0, math.radians(yaw_deg)))


def test_a_null_distance_is_a_tool_error_not_a_crash(backend: NemotronStackBackend) -> None:
    replies = iter(
        [
            {"tool": "check_reach", "args": {"bearing_deg": 10, "distance_m": None, "height_m": None}},
            {"action": "wait", "args": {}},
        ]
    )
    backend._call = lambda *a, **k: next(replies)  # type: ignore[method-assign]
    backend.decide(_obs(), "menu")
    assert "error" in backend._last_tool_result


@pytest.mark.parametrize("bad", [None, "about a metre", float("nan")])
def test_unusable_distances_all_become_the_same_error(backend: NemotronStackBackend, bad: object) -> None:
    replies = iter([{"tool": "check_reach", "args": {"distance_m": bad}}, {"action": "wait", "args": {}}])
    backend._call = lambda *a, **k: next(replies)  # type: ignore[method-assign]
    backend.decide(_obs(), "menu")
    assert "no pose or distance" in backend._last_tool_result


def _search(backend: NemotronStackBackend, calls: int) -> list[float]:
    """Call explore_frontier `calls` times, turning as told each time."""
    yaw, turns = 0.0, []
    for _ in range(calls):
        turn = backend._explore_frontier(_obs(yaw))["turn_degrees"]
        turns.append(turn)
        yaw += turn
    return turns


def test_the_first_lap_sees_every_direction_once(backend: NemotronStackBackend) -> None:
    turns = _search(backend, 5)
    assert all(t != 0 for t in turns)
    headings = {round((sum(turns[:i]) + 180) % 360 - 180) % 360 for i in range(6)}
    assert headings == {0, 60, 120, 180, 240, 300}


def test_after_a_full_lap_the_search_keeps_turning(backend: NemotronStackBackend) -> None:
    turns = _search(backend, 14)
    assert all(t != 0 for t in turns), turns


def test_the_unscanned_count_never_goes_negative(backend: NemotronStackBackend) -> None:
    yaw = 0.0
    for _ in range(10):
        out = backend._explore_frontier(_obs(yaw))
        assert not out["note"].startswith("-")
        yaw += out["turn_degrees"]


def test_the_model_cannot_overwrite_a_release_record() -> None:
    s = _TaskStack()
    s.note_released("test_item", (1.0, 2.0, 0.0), elapsed_s=5.0)
    s.apply({"facts": {"released:test_item": "false", "cup_colour": "red"}})
    assert s.facts["released:test_item"] == "true,at(1.0,2.0),t=5s"
    assert s.facts["cup_colour"] == "red"
