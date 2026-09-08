# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the people overlay (cv2 only, no ROS).

The overlay is what makes a tag in the text mean a person in the picture, so
the tests decode the drawn JPEG and look at the pixels: boxes land where the
per-mille coordinates say, the colour carries the identity state, and a frame
the model must not be given boxes for comes back untouched.
"""

from typing import cast

import cv2
import numpy as np
import pytest

from brain_client.brain import overlay
from brain_client.people.types import PeopleSnapshotDict

BACKGROUND = (40, 40, 40)  # BGR, dark and flat: any drawing stands out of it


def frame_jpeg(width: int = 640, height: int = 480) -> bytes:
    frame = np.full((height, width, 3), BACKGROUND, np.uint8)
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
    assert ok
    return encoded.tobytes()


def decode(jpeg: bytes) -> np.ndarray:
    frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert frame is not None
    return frame


def person(**overrides) -> dict:
    base = {"tag": "P3", "name": "Theo", "state": "known", "bbox": [200, 300, 800, 700], "head_bbox": None}
    return {**base, **overrides}


def snapshot(*people: dict) -> PeopleSnapshotDict:
    return cast("PeopleSnapshotDict", {"schema": 1, "stamp": 0.0, "image_size": [640, 480], "people": list(people)})


def changed(before: np.ndarray, after: np.ndarray) -> np.ndarray:
    return cv2.absdiff(before, after).max(axis=2)


def test_the_box_is_drawn_where_the_per_mille_coordinates_say():
    jpeg = frame_jpeg()
    drawn = overlay.draw_people(jpeg, snapshot(person(bbox=[200, 300, 800, 700])))
    assert drawn is not None and drawn != jpeg

    before, after = decode(jpeg), decode(drawn)
    assert after.shape == before.shape
    diff = changed(before, after)
    top, left, bottom, right = 96, 192, 384, 448  # 200/800 of 480, 300/700 of 640

    assert diff[top - 1 : top + 2, left + 20 : right - 20].max() > 40  # top edge
    assert diff[bottom - 2 : bottom + 1, left + 20 : right - 20].max() > 40  # bottom edge
    assert diff[top + 20 : bottom - 20, left - 1 : left + 2].max() > 40  # left edge
    assert diff[top + 20 : bottom - 20, right - 2 : right + 1].max() > 40  # right edge
    # Inside the box and far outside it, only JPEG noise on a flat colour.
    assert diff[top + 30 : bottom - 30, left + 30 : right - 30].max() < 20
    assert diff[400:479, 0:150].max() < 20


def test_the_tag_is_drawn_above_the_box():
    jpeg = frame_jpeg()
    drawn = overlay.draw_people(jpeg, snapshot(person(bbox=[400, 300, 800, 700])))
    assert drawn is not None
    diff = changed(decode(jpeg), decode(drawn))
    top, left = 192, 192
    assert diff[top - 20 : top, left : left + 60].max() > 40  # the label band


def test_a_person_at_the_top_of_the_frame_keeps_their_label_on_screen():
    jpeg = frame_jpeg()
    drawn = overlay.draw_people(jpeg, snapshot(person(bbox=[0, 0, 500, 300])))
    assert drawn is not None
    after = decode(drawn)
    assert after.shape == (480, 640, 3)  # nothing drawn off-frame, nothing crashed
    assert changed(decode(jpeg), after)[0:40, 0:100].max() > 40


def test_the_colour_carries_the_state():
    jpeg = frame_jpeg()
    edges = {}
    for state in ("known", "possible", "conflict"):
        drawn = overlay.draw_people(jpeg, snapshot(person(state=state, bbox=[200, 300, 800, 700])))
        assert drawn is not None
        after = decode(drawn)
        edges[state] = after[382:385, 250:400].reshape(-1, 3).mean(axis=0)  # the bottom edge line

    blue, green, red = edges["known"]  # teal
    assert blue > 100 and green > 100 and red < 100
    blue, green, red = edges["possible"]  # amber
    assert red > 140 and green > 100 and blue < 100
    blue, green, red = edges["conflict"]  # red
    assert red > 140 and green < 110 and blue < 110


def test_nothing_to_draw_returns_the_frame_untouched():
    jpeg = frame_jpeg()
    assert overlay.draw_people(jpeg, snapshot()) is jpeg
    assert overlay.draw_people(jpeg, snapshot(person(bbox=[], head_bbox=None))) is jpeg
    # A degenerate box is not a position: it must not re-encode a "drawn" frame.
    assert overlay.draw_people(jpeg, snapshot(person(bbox=[500, 500, 500, 500]))) is jpeg


def test_a_track_that_left_view_is_never_drawn():
    """A lost track's box is where the tracker thinks the person would be, kept
    for re-association; drawn, it puts a name on an empty patch of the picture
    (the webapp overlay drops it for the same reason)."""
    jpeg = frame_jpeg()
    assert overlay.draw_people(jpeg, snapshot(person(lost=True))) is jpeg
    drawn = overlay.draw_people(jpeg, snapshot(person(lost=True), person(tag="P4", bbox=[100, 100, 400, 400])))
    assert drawn is not None and drawn != jpeg
    changes = changed(decode(jpeg), decode(drawn))
    assert changes[400:800, 300:700].max() < 40  # the lost person's box is not there


def test_a_head_box_is_used_when_there_is_no_body_box():
    jpeg = frame_jpeg()
    drawn = overlay.draw_people(jpeg, snapshot(person(bbox=None, head_bbox=[200, 300, 400, 500])))
    assert drawn is not None and drawn != jpeg
    assert changed(decode(jpeg), decode(drawn))[95:98, 200:300].max() > 40


def test_an_undecodable_frame_reports_failure_instead_of_guessing():
    assert overlay.draw_people(b"not a jpeg", snapshot(person())) is None


def test_the_drawn_frame_is_still_a_readable_jpeg_of_the_same_size():
    jpeg = frame_jpeg(width=1280, height=720)
    drawn = overlay.draw_people(jpeg, snapshot(person(), person(tag="P4", name=None, state="unknown")))
    assert drawn is not None
    assert decode(drawn).shape == (720, 1280, 3)
    assert drawn[:2] == b"\xff\xd8"  # SOI: a JPEG, not a raw buffer


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
