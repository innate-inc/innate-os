# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The RFC 4.5 gates: which frames are allowed to carry identity evidence.

Synthetic crops only — a painted checkerboard is sharp, its blurred copy is
not, and that is the whole measurement.
"""

import cv2
import numpy as np
import pytest

from brain_client.people.quality import (
    BODY_MIN_MATCH_PX,
    BODY_MIN_OUTFIT_PX,
    BODY_MIN_SCORE,
    DRIVE_SUPPRESS_SEC,
    FACE_MIN_DETECT_PX,
    FACE_MIN_ENROL_PX,
    FACE_MIN_MATCH_PX,
    HEAD_PITCH_EPS_DEG,
    LUMINANCE_MAX,
    LUMINANCE_MIN,
    YAW_RATE_MAX,
    EgoMotion,
    EgoMotionTracker,
    IndependenceGate,
    Purpose,
    body_quality_score,
    body_score_ok,
    body_sharp_enough,
    body_size_ok,
    box_movement,
    crop_luminance,
    crop_sharpness,
    face_detectable,
    face_pose_ok,
    face_sharp_enough,
    face_size_ok,
    independent,
    luminance_ok,
    min_face_sharpness,
    pose_bucket,
    quality_score,
)


def checkerboard(size: int = 112, square: int = 8, value: int = 200) -> np.ndarray:
    image = np.zeros((size, size, 3), np.uint8)
    for row in range(0, size, square):
        for col in range(0, size, square):
            if (row // square + col // square) % 2 == 0:
                image[row : row + square, col : col + square] = value
    return image


# ------------------------------------------------------------- size gates


def test_a_face_below_the_detect_floor_is_not_even_a_detection():
    assert not face_detectable(FACE_MIN_DETECT_PX - 0.1)
    assert face_detectable(FACE_MIN_DETECT_PX)


def test_matching_needs_forty_native_pixels_of_face():
    assert not face_size_ok(FACE_MIN_MATCH_PX - 0.1)
    assert face_size_ok(FACE_MIN_MATCH_PX)


def test_enrolling_needs_more_face_than_matching():
    assert face_size_ok(FACE_MIN_ENROL_PX - 1, Purpose.MATCH)
    assert not face_size_ok(FACE_MIN_ENROL_PX - 1, Purpose.ENROL)


def test_body_matching_and_outfit_templates_have_different_floors():
    assert body_size_ok(BODY_MIN_MATCH_PX)
    assert not body_size_ok(BODY_MIN_OUTFIT_PX - 1, Purpose.ENROL)
    assert body_size_ok(BODY_MIN_OUTFIT_PX, Purpose.ENROL)


def test_body_detection_confidence_gate():
    assert body_score_ok(BODY_MIN_SCORE)
    assert not body_score_ok(BODY_MIN_SCORE - 0.01)


# ------------------------------------------------------------- pose gates


@pytest.mark.parametrize(("yaw", "pitch", "ok"), [(0, 0, True), (44, 39, True), (46, 0, False), (0, 41, False)])
def test_matching_pose_limits(yaw, pitch, ok):
    assert face_pose_ok(yaw, pitch) is ok


@pytest.mark.parametrize(("yaw", "pitch", "ok"), [(29, 24, True), (31, 0, False), (0, 26, False)])
def test_enrolment_pose_limits_are_tighter(yaw, pitch, ok):
    assert face_pose_ok(yaw, pitch, Purpose.ENROL) is ok


def test_pose_limits_are_symmetric_in_sign():
    assert face_pose_ok(-44, -39)
    assert not face_pose_ok(-46, 0)


@pytest.mark.parametrize(
    ("yaw", "pitch", "bucket"),
    [(0, 0, "frontal"), (30, 0, "right"), (-30, 0, "left"), (0, 30, "up"), (0, -30, "down"), (30, 30, "up")],
)
def test_pose_buckets(yaw, pitch, bucket):
    assert pose_bucket(yaw, pitch) == bucket


# --------------------------------------------------------- pixel measures


def test_a_checkerboard_is_sharper_than_its_blurred_copy():
    sharp = checkerboard()
    blurred = cv2.GaussianBlur(sharp, (15, 15), 0)
    assert crop_sharpness(sharp) > crop_sharpness(blurred)


def test_sharpness_of_an_empty_crop_is_zero():
    assert crop_sharpness(np.zeros((0, 0, 3), np.uint8)) == 0.0


def test_sharpness_of_a_flat_crop_is_zero():
    assert crop_sharpness(np.full((64, 64, 3), 128, np.uint8)) == pytest.approx(0.0, abs=1e-6)


def test_luminance_reads_the_mean_grey_level():
    assert crop_luminance(np.full((32, 32, 3), 100, np.uint8)) == pytest.approx(100.0, abs=1.0)


def test_a_dark_room_fails_the_luminance_gate():
    assert not luminance_ok(LUMINANCE_MIN - 1)
    assert not luminance_ok(LUMINANCE_MAX + 1)
    assert luminance_ok((LUMINANCE_MIN + LUMINANCE_MAX) / 2)


def test_the_sharpness_floor_scales_down_with_face_size():
    assert min_face_sharpness(FACE_MIN_ENROL_PX / 2) < min_face_sharpness(FACE_MIN_ENROL_PX)
    assert min_face_sharpness(4 * FACE_MIN_ENROL_PX) == min_face_sharpness(FACE_MIN_ENROL_PX)


def test_a_blurred_face_fails_the_sharpness_gate_at_its_own_size():
    blurred = crop_sharpness(cv2.GaussianBlur(checkerboard(), (31, 31), 8))
    assert not face_sharp_enough(blurred, 64.0)
    assert face_sharp_enough(crop_sharpness(checkerboard()), 64.0)


def test_body_sharpness_gate():
    assert body_sharp_enough(crop_sharpness(checkerboard()))
    assert not body_sharp_enough(0.0)


# --------------------------------------------------------------- independence


def test_box_movement_is_relative_to_the_box_own_size():
    small = ((0.0, 0.0, 0.1, 0.1), (0.0, 0.02, 0.1, 0.12))
    large = ((0.0, 0.0, 0.8, 0.8), (0.0, 0.02, 0.8, 0.82))
    assert box_movement(*small) > box_movement(*large)


def test_frames_closer_than_one_hundred_and_fifty_milliseconds_are_never_independent():
    box = (0.1, 0.1, 0.5, 0.3)
    assert not independent(1.10, box, "frontal", last_stamp=1.0, last_box=box, last_bucket="frontal")


def test_a_still_person_in_the_same_pose_bucket_is_a_near_duplicate():
    box = (0.1, 0.1, 0.5, 0.3)
    assert not independent(2.0, box, "frontal", last_stamp=1.0, last_box=box, last_bucket="frontal")


def test_a_different_pose_bucket_makes_a_frame_independent():
    box = (0.1, 0.1, 0.5, 0.3)
    assert independent(2.0, box, "left", last_stamp=1.0, last_box=box, last_bucket="frontal")


def test_a_moved_box_makes_a_frame_independent():
    last = (0.1, 0.1, 0.5, 0.3)
    moved = (0.1, 0.2, 0.5, 0.4)
    assert independent(2.0, moved, "frontal", last_stamp=1.0, last_box=last, last_bucket="frontal")


def test_the_independence_gate_accepts_the_first_frame_of_a_track():
    gate = IndependenceGate()
    assert gate.accept("P1", 1.0, (0.1, 0.1, 0.5, 0.3), "frontal")


def test_the_independence_gate_rejects_the_duplicate_that_follows():
    gate = IndependenceGate()
    box = (0.1, 0.1, 0.5, 0.3)
    gate.accept("P1", 1.0, box, "frontal")
    assert not gate.accept("P1", 1.05, box, "frontal")


def test_the_independence_gate_keeps_tracks_apart():
    gate = IndependenceGate()
    box = (0.1, 0.1, 0.5, 0.3)
    gate.accept("P1", 1.0, box, "frontal")
    assert gate.accept("P2", 1.0, box, "frontal")


def test_forgetting_a_track_resets_its_independence_memory():
    gate = IndependenceGate()
    box = (0.1, 0.1, 0.5, 0.3)
    gate.accept("P1", 1.0, box, "frontal")
    gate.forget("P1")
    assert gate.accept("P1", 1.05, box, "frontal")


# ------------------------------------------------------------ quality score


def _score(**overrides) -> float:
    args = {
        "size_px": 64.0,
        "yaw_deg": 0.0,
        "pitch_deg": 0.0,
        "sharpness": 200.0,
        "luminance": 130.0,
    }
    args.update(overrides)
    return quality_score(**args)


def test_quality_score_is_bounded_to_zero_one():
    assert 0.0 <= _score() <= 1.0
    assert 0.0 <= _score(size_px=8.0, sharpness=0.0, luminance=0.0) <= 1.0


def test_a_bigger_sharper_face_scores_higher():
    assert _score(size_px=96.0) > _score(size_px=30.0)
    assert _score(sharpness=200.0) > _score(sharpness=13.0)


def test_a_turned_head_scores_lower_than_a_frontal_one():
    assert _score(yaw_deg=40.0) < _score(yaw_deg=0.0)
    assert _score(pitch_deg=35.0) < _score(pitch_deg=0.0)


def test_bad_exposure_costs_score():
    assert _score(luminance=45.0) < _score(luminance=130.0)


def test_a_face_quality_model_only_ever_lowers_the_weight():
    assert quality_score(size_px=64, yaw_deg=0, pitch_deg=0, sharpness=200, luminance=130, fiqa=0.5) == pytest.approx(
        0.5 * _score()
    )


def test_body_quality_score_rewards_a_tall_sharp_crop():
    tall = body_quality_score(height_px=300.0, score=0.9, sharpness=200.0)
    short = body_quality_score(height_px=100.0, score=0.9, sharpness=200.0)
    assert 0.0 <= short < tall <= 1.0


# --------------------------------------------------------------- ego-motion


def test_a_still_robot_allows_decisions():
    assert EgoMotion.still_at(100.0).still


def test_a_recent_drive_command_suppresses_decisions():
    ego = EgoMotion(stamp=100.0, last_drive=100.0 - DRIVE_SUPPRESS_SEC / 2)
    assert ego.recently_driven and not ego.still


def test_an_old_drive_command_no_longer_suppresses():
    ego = EgoMotion(stamp=100.0, last_drive=100.0 - DRIVE_SUPPRESS_SEC - 0.1)
    assert not ego.recently_driven and ego.still


def test_a_moving_head_suppresses_decisions():
    assert not EgoMotion(stamp=100.0, head_pitch_delta_deg=HEAD_PITCH_EPS_DEG).still


def test_a_turning_base_suppresses_decisions():
    assert not EgoMotion(stamp=100.0, yaw_rate=YAW_RATE_MAX).still


def test_the_tracker_records_a_nonzero_cmd_vel():
    tracker = EgoMotionTracker()
    tracker.note_cmd_vel(10.0, 0.0, 0.0, 0.3)
    assert not tracker.state(10.5).still


def test_the_tracker_ignores_a_zero_cmd_vel():
    tracker = EgoMotionTracker()
    tracker.note_cmd_vel(10.0, 0.0, 0.0, 0.0)
    assert tracker.state(10.5).still


def test_the_tracker_reports_a_head_step_then_forgets_it():
    tracker = EgoMotionTracker()
    tracker.note_head_pitch(10.0, 0.0)
    tracker.note_head_pitch(10.2, 5.0)
    assert not tracker.state(10.3).still
    assert tracker.state(10.2 + DRIVE_SUPPRESS_SEC + 0.1).still


def test_the_tracker_carries_the_latest_head_pitch_into_the_state():
    tracker = EgoMotionTracker()
    tracker.note_head_pitch(10.0, -12.0)
    assert tracker.state(11.0).head_pitch_deg == -12.0


def test_the_tracker_reports_odometry_yaw_rate():
    tracker = EgoMotionTracker()
    tracker.note_yaw_rate(10.0, 0.5)
    assert not tracker.state(10.1).still
