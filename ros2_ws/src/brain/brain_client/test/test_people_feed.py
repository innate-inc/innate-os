# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The one gate between the people node and a turn.

``/brain/people`` is latched TRANSIENT_LOCAL, so a payload the block or the
overlay cannot render is not one bad turn — it is replayed to every restart
until the node publishes again. Everything below is about what must never get
past this file. ``people_feed`` imports rclpy and std_msgs at module level, so
they are fabricated here for the duration of the import (the same
``sys.meta_path`` stub ``test_people_node.py`` uses; a real installation wins).
"""

from __future__ import annotations

import importlib.abc
import importlib.util
import json
import sys
import time
from collections.abc import MutableSequence
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

_ROS_ROOTS = frozenset({"rclpy", "std_msgs"})


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

from brain_client.perception.people_feed import PeopleFeed, decode_event_image  # noqa: E402 — needs the stubs above

# The stubs live exactly as long as the import above: every other test module in
# this session must go on finding ROS missing, because it is.
sys.meta_path.remove(_STUB_FINDER)
for _stubbed in [name for name, module in sys.modules.items() if isinstance(module, _StubModule)]:
    del sys.modules[_stubbed]


def person(tag: str = "P3", **overrides) -> dict:
    base = {
        "tag": tag,
        "person_id": "person_7f92a1b3",
        "name": "Theo",
        "state": "known",
        "bbox": [100, 300, 930, 560],
        "head_bbox": [100, 380, 260, 480],
        "range_m": 1.8,
        "bearing_deg": -4.0,
        "tracked_sec": 41.2,
        "lost": False,
        "lost_sec": None,
    }
    return {**base, **overrides}


def snapshot(people: list[dict] | None = None, **overrides) -> dict:
    base = {
        "schema": 1,
        "stamp": time.time(),
        "frame_stamp_ns": "1788818400123456789",
        "image_size": [640, 480],
        "health": {"camera": "ok"},
        "collection_enabled": True,
        "attention": None,
        "people": people if people is not None else [person()],
        "recent": [],
    }
    return {**base, **overrides}


def feed() -> PeopleFeed:
    return PeopleFeed(MagicMock())


def publish(made: PeopleFeed, payload) -> None:
    made._on_snapshot(SimpleNamespace(data=payload if isinstance(payload, str) else json.dumps(payload)))


def test_a_snapshot_of_this_schema_is_read():
    made = feed()
    publish(made, snapshot())
    latest = made.latest()
    assert latest is not None and [p["tag"] for p in latest["people"]] == ["P3"]
    assert made.fresh(3.0) is latest


def test_a_snapshot_with_no_schema_is_not_a_snapshot():
    """The default used to be "assume it is ours", which let anything shaped
    like a dict through verbatim."""
    made = feed()
    payload = snapshot()
    del payload["schema"]
    publish(made, payload)
    assert made.latest() is None


def test_a_snapshot_of_another_schema_is_ignored():
    made = feed()
    publish(made, snapshot(schema=99))
    assert made.latest() is None


def test_malformed_json_is_ignored_rather_than_raised():
    made = feed()
    publish(made, "{not json")
    assert made.latest() is None


def test_a_person_whose_numbers_are_not_numbers_is_dropped_and_the_rest_survive():
    """The block prints a box through int() and the overlay draws it the same
    way; one entry that raises there would cost every turn until a reboot."""
    made = feed()
    publish(
        made,
        snapshot(
            [
                person(tag="P1", bbox="over there"),
                person(tag="P2", bbox=[1, 2, "three", 4]),
                person(tag="P4", tracked_sec="a while"),
                person(tag="P5"),
            ]
        ),
    )
    latest = made.latest()
    assert latest is not None and [p["tag"] for p in latest["people"]] == ["P5"]


def test_a_snapshot_with_no_usable_stamp_is_ignored():
    """Every reader ages the snapshot off this stamp, through float()."""
    made = feed()
    publish(made, snapshot(stamp="just now"))
    assert made.latest() is None
    publish(made, snapshot(stamp=None))
    assert made.latest() is None


def test_a_good_snapshot_is_kept_when_a_bad_one_follows_it():
    made = feed()
    publish(made, snapshot())
    publish(made, snapshot(schema=99))
    latest = made.latest()
    assert latest is not None and latest["schema"] == 1


def test_events_carry_no_schema_of_their_own():
    made = feed()
    seen: list[dict] = []
    made.on_event = seen.append
    made._on_event(SimpleNamespace(data=json.dumps({"kind": "name_learned", "text": "P1 = Ana"})))
    assert [event["kind"] for event in seen] == ["name_learned"]


def test_an_event_image_that_is_not_base64_is_no_image():
    assert decode_event_image({"image_b64": "not base64 %%%"}) is None
    assert decode_event_image({}) is None
