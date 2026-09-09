# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""What the gaze loop looks at while the people node is publishing.

The tracker around this is an rclpy module; the choice it makes is not, and it
is the whole point of not running a second face model in the brain.
"""

from typing import cast

import pytest

from brain_client.people.types import PeopleSnapshotDict
from brain_client.perception import gaze_targets

NOW = 1_788_818_400.0


def person(**overrides) -> dict:
    base = {
        "tag": "P3",
        "person_id": "person_7f92a1b3",
        "name": "Theo",
        "state": "known",
        "evidence": ["face"],
        "confidence": 0.91,
        "bbox": [100, 300, 930, 560],
        "head_bbox": [100, 380, 260, 480],
        "range_m": 1.8,
        "bearing_deg": -4.0,
        "tracked_sec": 41.2,
        "lost": False,
        "description": None,
        "hint": None,
        "learned": None,
        "digest": None,
    }
    return {**base, **overrides}


def snapshot(people: list[dict] | None = None, **overrides) -> PeopleSnapshotDict:
    base = {
        "schema": 1,
        "stamp": NOW,
        "frame_stamp_ns": "1788818400123456789",
        "image_size": [640, 480],
        "health": {"camera": "ok"},
        "collection_enabled": True,
        "attention": None,
        "people": people or [],
        "recent": [],
    }
    return cast("PeopleSnapshotDict", {**base, **overrides})


def test_the_attention_target_is_what_the_head_follows():
    attention = {"tag": "P4", "text": "trying to see P4's face", "head_bbox": [120, 400, 220, 480]}
    box = gaze_targets.gaze_box(snapshot([person()], attention=attention))
    assert box == pytest.approx((0.12, 0.40, 0.22, 0.48))


def test_everyone_settled_still_leaves_the_nearest_person_to_look_at():
    """choose_attention names nobody once every face is confirmed. Falling
    through to the detector there is what loads the second face model."""
    near = person(tag="P3", range_m=1.2, head_bbox=[100, 380, 260, 480])
    far = person(tag="P4", range_m=4.0, head_bbox=[300, 100, 380, 180])
    box = gaze_targets.gaze_box(snapshot([far, near]))
    assert box == pytest.approx((0.10, 0.38, 0.26, 0.48))


def test_a_person_whose_face_was_never_located_is_tracked_by_the_top_of_their_box():
    box = gaze_targets.gaze_box(snapshot([person(head_bbox=None, bbox=[100, 300, 900, 560])]))
    assert box == pytest.approx((0.10, 0.30, 0.42, 0.56))


def test_a_person_with_no_range_yet_sorts_behind_one_that_has_it():
    unmeasured = person(tag="P3", range_m=None, head_bbox=[300, 100, 380, 180])
    measured = person(tag="P4", range_m=3.5, head_bbox=[100, 380, 260, 480])
    box = gaze_targets.gaze_box(snapshot([unmeasured, measured]))
    assert box == pytest.approx((0.10, 0.38, 0.26, 0.48))


def test_a_lost_track_is_not_worth_looking_at():
    assert gaze_targets.gaze_box(snapshot([person(lost=True)])) is None


def test_an_empty_scene_gives_the_loop_nothing_to_aim_at():
    assert gaze_targets.gaze_box(snapshot([])) is None


def test_a_malformed_box_is_ignored_rather_than_aimed_at():
    assert gaze_targets.gaze_box(snapshot([person(head_bbox=[100, 380], bbox=None)])) is None


def test_a_box_becomes_the_centre_and_size_the_controller_steers_on():
    face = gaze_targets.as_face((0.10, 0.38, 0.26, 0.48))
    assert face["center_x"] == pytest.approx(0.43)
    assert face["center_y"] == pytest.approx(0.18)
    assert face["width"] == pytest.approx(0.10)
    assert face["height"] == pytest.approx(0.16)
