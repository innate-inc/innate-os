# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Which of the two ways of finding a face the gaze loop takes.

The people node already detected everyone on this same stream, so the brain
must load InspireFace only when that feed is really gone — an empty room is the
common case, and the node says "nobody" at a heartbeat, not at 5 Hz.
``gaze`` imports rclpy and the message packages at module level, so they are
fabricated here for the duration of the import (the same ``sys.meta_path`` stub
``test_people_node.py`` uses; a real installation wins).
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import sys
import time
from collections.abc import MutableSequence
from types import ModuleType
from unittest.mock import MagicMock

import pytest

_ROS_ROOTS = frozenset({"geometry_msgs", "nav2_simple_commander", "rclpy", "sensor_msgs", "std_msgs", "std_srvs"})


class _StubModule(ModuleType):
    """A module whose every attribute is a mock, and which is a package so the
    submodule imports below it keep resolving."""

    __path__: MutableSequence[str] = []

    def __getattr__(self, name: str) -> MagicMock:
        value = MagicMock()
        setattr(self, name, value)
        return value


class _StubLoader(importlib.abc.Loader):
    def create_module(self, spec) -> ModuleType:
        return _StubModule(spec.name)

    def exec_module(self, module: ModuleType) -> None:
        pass


class _StubFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname: str, path=None, target=None):
        if fullname.partition(".")[0] not in _ROS_ROOTS:
            return None
        return importlib.util.spec_from_loader(fullname, _StubLoader())


# Appended, never inserted: where ROS is installed its own finder answers first.
_STUB_FINDER = _StubFinder()
sys.meta_path.append(_STUB_FINDER)

from brain_client.perception import gaze  # noqa: E402 — needs the stubs above

# The stubs live exactly as long as the import above: every other test module in
# this session must go on finding ROS missing, because it is.
sys.meta_path.remove(_STUB_FINDER)
for _stubbed in [name for name, module in sys.modules.items() if isinstance(module, _StubModule)]:
    del sys.modules[_stubbed]


class FakeFeed:
    """PeopleFeed's read side: the latest snapshot, if it is young enough."""

    def __init__(self, snapshot: dict | None):
        self._snapshot = snapshot

    def fresh(self, max_age_sec: float) -> dict | None:
        if self._snapshot is None or time.time() - float(self._snapshot.get("stamp") or 0.0) > max_age_sec:
            return None
        return self._snapshot


def snapshot(age_sec: float, people: list[dict] | None = None) -> dict:
    return {
        "schema": 1,
        "stamp": time.time() - age_sec,
        "image_size": [640, 480],
        "attention": None,
        "people": people or [],
        "recent": [],
    }


def person() -> dict:
    return {"tag": "P3", "bbox": [100, 300, 930, 560], "head_bbox": [100, 380, 260, 480], "range_m": 1.8}


@pytest.fixture
def tracker(monkeypatch: pytest.MonkeyPatch):
    """The real tracker around a mock node, with the two things that reach
    hardware recorded rather than done."""

    def build(feed) -> tuple[gaze.ROSPersonTracker, list[str]]:
        made = gaze.ROSPersonTracker(MagicMock(), people=feed)
        calls: list[str] = []
        monkeypatch.setattr(made, "_ensure_detector", lambda: calls.append("detector"))
        monkeypatch.setattr(made._gaze, "recenter", lambda: calls.append("recenter"))
        monkeypatch.setattr(made._gaze, "track_face", lambda face: calls.append("track"))
        return made, calls

    return build


def test_an_idle_people_node_is_not_a_reason_to_load_a_second_face_model(tracker):
    """The node heartbeats every 5 s with nobody in view, so a 4 s-old empty
    snapshot is a live feed saying the room is empty — not a missing one."""
    made, calls = tracker(FakeFeed(snapshot(4.0)))
    made._track_once()
    assert calls == ["recenter"]


def test_a_feed_that_has_said_nothing_for_three_heartbeats_earns_the_detector(tracker):
    made, calls = tracker(FakeFeed(snapshot(gaze._FEED_ABSENT_SEC + 1.0)))
    made._track_once()
    assert "detector" in calls


def test_no_people_node_at_all_falls_back_to_detection(tracker):
    made, calls = tracker(None)
    made._track_once()
    assert "detector" in calls


def test_a_fresh_snapshot_with_somebody_in_it_is_the_whole_answer(tracker):
    made, calls = tracker(FakeFeed(snapshot(0.1, [person()])))
    made._track_once()
    assert calls == ["track"]
