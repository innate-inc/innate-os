# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""ByteTrack-style association, tags, lost-track memory and splits."""

import numpy as np
import pytest

from brain_client.people.quality import EgoMotion
from brain_client.people.track import Track, Tracker, TrackerConfig, cosine, iou
from brain_client.people.types import Detection

BOX = (0.20, 0.40, 0.80, 0.55)


def detection(box=BOX, score: float = 0.9) -> Detection:
    return Detection(box=box, score=score)


def shifted(box, dx: float = 0.0, dy: float = 0.0):
    ymin, xmin, ymax, xmax = box
    return (ymin + dy, xmin + dx, ymax + dy, xmax + dx)


def unit(*values: float) -> np.ndarray:
    vector = np.array(values, dtype=np.float32)
    return vector / float(np.linalg.norm(vector))


# ----------------------------------------------------------------- helpers


def test_iou_of_a_box_with_itself_is_one():
    assert iou(BOX, BOX) == pytest.approx(1.0)


def test_iou_of_disjoint_boxes_is_zero():
    assert iou(BOX, shifted(BOX, dx=0.5)) == 0.0


def test_iou_of_a_half_overlap():
    assert iou((0.0, 0.0, 1.0, 1.0), (0.0, 0.5, 1.0, 1.5)) == pytest.approx(1 / 3)


def test_cosine_of_orthogonal_embeddings_is_zero():
    assert cosine(unit(1, 0), unit(0, 1)) == pytest.approx(0.0)


def test_cosine_of_an_empty_embedding_is_zero():
    assert cosine(np.zeros(0, np.float32), unit(1, 0)) == 0.0


def test_cosine_of_mismatched_shapes_is_zero():
    assert cosine(unit(1, 0), unit(1, 0, 0)) == 0.0


# -------------------------------------------------------------------- tags


def test_the_first_person_seen_is_p1():
    tracker = Tracker()
    tracks = tracker.update([detection()], 100.0)
    assert [t.tag for t in tracks] == ["P1"]


def test_a_second_person_gets_the_next_tag():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracks = tracker.update([detection(), detection(shifted(BOX, dx=0.3))], 100.2)
    assert sorted(t.tag for t in tracks) == ["P1", "P2"]


def test_a_tag_is_never_reused_after_its_track_dies():
    tracker = Tracker(TrackerConfig(track_memory_sec=1.0))
    tracker.update([detection()], 100.0)
    tracker.update([], 110.0)  # P1 misses its window and goes lost
    tracker.update([], 112.0)  # and then out of memory entirely
    assert tracker.all_tracks() == []
    tracks = tracker.update([detection()], 113.0)
    assert [t.tag for t in tracks] == ["P2"]


def test_a_tracked_person_keeps_their_tag_across_frames():
    tracker = Tracker()
    tracks = tracker.update([detection()], 100.0)
    for step in range(1, 6):
        tracks = tracker.update([detection(shifted(BOX, dx=0.01 * step))], 100.0 + 0.2 * step)
    assert [t.tag for t in tracks] == ["P1"]
    assert tracks[0].hits == 6


# ------------------------------------------------------------- association


def test_constant_velocity_prediction_bridges_a_jump_the_raw_box_would_miss():
    tracker = Tracker()
    tracker.update([detection((0.2, 0.10, 0.8, 0.25))], 100.0)
    tracker.update([detection((0.2, 0.14, 0.8, 0.29))], 100.2)
    tracker.update([detection((0.2, 0.18, 0.8, 0.33))], 100.4)
    # A 0.09 jump overlaps the last box too little to match on its own; the
    # velocity carried from the two steps before it closes the gap.
    tracks = tracker.update([detection((0.2, 0.27, 0.8, 0.42))], 100.6)
    assert [t.tag for t in tracks] == ["P1"]


def test_the_same_jump_without_a_velocity_history_starts_a_new_track():
    tracker = Tracker()
    tracker.update([detection((0.2, 0.18, 0.8, 0.33))], 100.4)
    tracks = tracker.update([detection((0.2, 0.27, 0.8, 0.42))], 100.6)
    assert len(tracks) == 2


def test_prediction_moves_the_box_forward_in_time():
    track = Track(tag="P1", box=(0.0, 0.0, 0.2, 0.2), score=0.9, first_seen=0.0, last_seen=0.0, velocity=(0.1, 0.0))
    assert track.predicted(2.0)[1] == pytest.approx(0.2)


def test_the_low_score_pass_keeps_a_blurred_detection_attached():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracks = tracker.update([detection(shifted(BOX, dx=0.01), score=0.2)], 100.2)
    assert [t.tag for t in tracks] == ["P1"]
    assert tracks[0].last_seen == 100.2


def test_a_low_score_detection_alone_never_starts_a_track():
    tracker = Tracker()
    assert tracker.update([detection(score=0.2)], 100.0) == []


def test_a_detection_below_the_low_score_floor_is_ignored_entirely():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracks = tracker.update([detection(score=0.05)], 100.2)
    assert tracks[0].last_seen == 100.0  # nothing matched it


def test_two_people_crossing_keep_their_own_tags():
    tracker = Tracker()
    left, right = (0.2, 0.05, 0.8, 0.20), (0.2, 0.60, 0.8, 0.75)
    tracker.update([detection(left), detection(right)], 100.0)
    tags = {}
    for step in range(1, 5):
        moved_left = shifted(left, dx=0.05 * step)
        moved_right = shifted(right, dx=-0.05 * step)
        tracks = tracker.update([detection(moved_left), detection(moved_right)], 100.0 + 0.2 * step)
        tags = {t.tag: t.box[1] for t in tracks}
    assert len(tags) == 2


def test_ego_motion_loosens_the_association_gate():
    still = Tracker()
    still.update([detection((0.2, 0.10, 0.8, 0.25))], 100.0)
    moving = Tracker()
    moving.update([detection((0.2, 0.10, 0.8, 0.25))], 100.0)
    jumped = detection((0.2, 0.20, 0.8, 0.35))
    driving = EgoMotion(stamp=100.2, last_drive=100.1)
    assert len(still.update([jumped], 100.2)) == 2  # a new track, the jump was too big
    assert len(moving.update([jumped], 100.2, driving)) == 1


# -------------------------------------------------------------- lost tracks


def test_a_track_goes_lost_after_the_miss_window_and_stays_in_memory():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracker.update([], 102.0)
    assert tracker.live() == []
    assert [t.tag for t in tracker.lost()] == ["P1"]


def test_a_lost_track_expires_after_the_memory_window():
    tracker = Tracker(TrackerConfig(track_memory_sec=300.0))
    tracker.update([detection()], 100.0)
    tracker.update([], 102.0)
    tracker.update([], 500.0)
    assert tracker.all_tracks() == []


def test_a_lost_track_recovered_by_overlap_reports_itself_as_recovered():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracker.update([], 102.0)
    tracks = tracker.update([detection()], 102.5)
    assert [t.tag for t in tracks] == ["P1"]
    assert tracker.take_recovered() == ["P1"]


def test_take_recovered_drains_its_list():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracker.update([], 102.0)
    tracker.update([detection()], 102.5)
    tracker.take_recovered()
    assert tracker.take_recovered() == []


def test_a_track_that_only_blinks_is_never_reported_as_recovered():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracker.update([], 100.5)
    tracker.update([detection()], 100.9)
    assert tracker.take_recovered() == []


# ------------------------------------------------------- re-association


def test_body_reassociation_restores_the_lost_tag():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracker.note_body("P1", unit(1, 0, 0), "osnet")
    tracker.update([], 102.0)
    tracker.update([detection(shifted(BOX, dx=0.4))], 200.0)
    assert tracker.reassociate("P2", unit(0.99, 0.14, 0.0), "osnet", 200.0) == "P1"
    assert [t.tag for t in tracker.live()] == ["P1"]


def test_a_reassociated_track_is_reported_as_recovered():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracker.note_body("P1", unit(1, 0, 0), "osnet")
    tracker.update([], 102.0)
    tracker.update([detection(shifted(BOX, dx=0.4))], 200.0)
    tracker.take_recovered()
    tracker.reassociate("P2", unit(1, 0, 0), "osnet", 200.0)
    assert tracker.take_recovered() == ["P1"]


def test_a_different_outfit_keeps_the_new_tag():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracker.note_body("P1", unit(1, 0, 0), "osnet")
    tracker.update([], 102.0)
    tracker.update([detection(shifted(BOX, dx=0.4))], 200.0)
    assert tracker.reassociate("P2", unit(0, 1, 0), "osnet", 200.0) is None
    assert {t.tag for t in tracker.live()} == {"P2"}


def test_embeddings_of_a_different_model_are_never_compared():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracker.note_body("P1", unit(1, 0, 0), "osnet")
    tracker.update([], 102.0)
    tracker.update([detection(shifted(BOX, dx=0.4))], 200.0)
    assert tracker.reassociate("P2", unit(1, 0, 0), "some-other-model", 200.0) is None


def test_reassociation_never_steals_a_live_track():
    tracker = Tracker()
    tracker.update([detection(), detection(shifted(BOX, dx=0.4))], 100.0)
    tracker.note_body("P1", unit(1, 0, 0), "osnet")
    assert tracker.reassociate("P2", unit(1, 0, 0), "osnet", 100.5) is None


def test_a_model_change_clears_the_body_buffer():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracker.note_body("P1", unit(1, 0, 0), "osnet")
    tracker.note_body("P1", unit(0, 1, 0), "other")
    track = tracker.get("P1")
    assert track is not None and len(track.body_templates) == 1


def test_an_empty_embedding_is_not_stored():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracker.note_body("P1", np.zeros(0, np.float32), "osnet")
    track = tracker.get("P1")
    assert track is not None and len(track.body_templates) == 0


# ------------------------------------------------------------------ splits


def test_split_retires_the_old_tag_and_issues_a_new_one():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    new_tag = tracker.split("P1")
    assert new_tag == "P2"
    assert tracker.get("P1") is None
    assert [t.tag for t in tracker.live()] == ["P2"]


def test_a_split_track_keeps_the_pixels_but_not_the_outfit_buffer():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracker.note_body("P1", unit(1, 0, 0), "osnet")
    new_tag = tracker.split("P1")
    assert new_tag is not None
    track = tracker.get(new_tag)
    assert track is not None and track.box == BOX and len(track.body_templates) == 0


def test_splitting_an_unknown_tag_does_nothing():
    assert Tracker().split("P9") is None


def test_forget_removes_a_track_outright():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracker.forget("P1")
    assert tracker.all_tracks() == []


def test_track_age_is_measured_from_first_sight():
    tracker = Tracker()
    tracker.update([detection()], 100.0)
    tracker.update([detection()], 103.0)
    track = tracker.get("P1")
    assert track is not None and track.age_sec == pytest.approx(3.0)
