# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pinhole geometry for the people engine (no ROS, no camera).

The anchor is the RFC 1.3 table: what this camera, 0.26 m off the floor with a
400.8 px native focal length, actually delivers of a standing adult.
"""

import math

import pytest

from brain_client.people.geometry import (
    CAMERA_HEIGHT_M,
    CX,
    CY,
    FX,
    FY,
    MAX_FLOOR_RANGE_M,
    NATIVE_FOCAL_PX,
    CameraModel,
    box_center,
    box_size,
    elevation_deg,
    eye_elevation_deg,
    feet_visible,
    head_region,
    native_pixel_size,
    native_to_published,
    pose_from_landmarks,
    published_to_native,
)

CENTER_X = CX / 640.0  # a box centred on the principal point has no bearing


def person_box(range_m: float, *, height_m: float = 1.70, camera: CameraModel | None = None) -> tuple:
    """The published-frame box of a standing person at ``range_m``, head level."""
    model = camera or CameraModel.published_default()
    feet_v = model.cy + model.fy * (model.camera_height_m / range_m)
    head_v = model.cy - model.fy * ((height_m - model.camera_height_m) / range_m)
    half_w = 0.3 * model.fx / range_m
    return (
        head_v / model.height,
        (model.cx - half_w) / model.width,
        feet_v / model.height,
        (model.cx + half_w) / model.width,
    )


# --------------------------------------------------------------- the mapping


def test_published_to_native_doubles_x_and_scales_y_by_three_halves():
    assert published_to_native(320.0, 240.0) == (640.0, 360.0)


def test_native_to_published_inverts_the_mapping():
    assert native_to_published(*published_to_native(101.0, 202.0)) == pytest.approx((101.0, 202.0))


def test_native_model_recovers_the_factory_focal_length():
    native = CameraModel.published_default().native()
    assert native.fx == pytest.approx(NATIVE_FOCAL_PX, abs=0.5)
    assert native.fy == pytest.approx(NATIVE_FOCAL_PX, abs=0.5)
    assert (native.width, native.height) == (1280, 720)


def test_native_model_keeps_the_dimensionless_distortion_coefficients():
    model = CameraModel(FX, FY, CX, CY, 640, 480, distortion=(-0.31, 0.09, 0.0, 0.0, 0.0))
    assert model.native().distortion == model.distortion


def test_camera_info_with_a_real_k_is_used_verbatim():
    k = [210.0, 0.0, 318.0, 0.0, 270.0, 244.0, 0.0, 0.0, 1.0]
    model = CameraModel.from_camera_info(k, 640, 480)
    assert (model.fx, model.fy, model.cx, model.cy) == (210.0, 270.0, 318.0, 244.0)


def test_camera_info_with_an_all_zero_k_falls_back_to_the_tape_measured_defaults():
    model = CameraModel.from_camera_info([0.0] * 9, 640, 480)
    assert (model.fx, model.fy, model.cx, model.cy) == (FX, FY, CX, CY)


def test_camera_info_fallback_scales_to_the_frame_size_it_was_given():
    model = CameraModel.from_camera_info(None, 1280, 960)
    assert model.fx == pytest.approx(2 * FX)
    assert model.fy == pytest.approx(2 * FY)


def test_k_matrix_is_the_row_major_intrinsic():
    model = CameraModel.published_default()
    assert model.k_matrix == ((FX, 0.0, CX), (0.0, FY, CY), (0.0, 0.0, 1.0))


# ------------------------------------------------------- the RFC 1.3 table


@pytest.mark.parametrize(
    ("range_m", "face_px", "body_px"),
    [(1.0, 64, 681), (1.5, 43, 454), (2.0, 32, 341), (2.5, 26, 272), (3.0, 21, 227), (5.0, 13, 136)],
)
def test_native_pixel_sizes_reproduce_the_rfc_table(range_m, face_px, body_px):
    assert native_pixel_size(0.16, range_m) == pytest.approx(face_px, abs=1.0)
    assert native_pixel_size(1.70, range_m) == pytest.approx(body_px, abs=1.0)


@pytest.mark.parametrize(("range_m", "degrees"), [(1.0, 53), (1.5, 42), (2.0, 34), (3.0, 24), (5.0, 15)])
def test_eye_elevation_reproduces_the_rfc_table(range_m, degrees):
    assert round(eye_elevation_deg(range_m)) == degrees


def test_elevation_of_the_camera_plane_is_zero():
    assert elevation_deg(CAMERA_HEIGHT_M, 2.0) == pytest.approx(0.0)


def test_native_pixel_size_of_a_zero_range_target_is_zero_rather_than_infinite():
    assert native_pixel_size(1.7, 0.0) == 0.0


# ------------------------------------------------------------ boxes and rays


def test_box_center_and_size():
    assert box_center((0.2, 0.4, 0.6, 0.8)) == pytest.approx((0.6, 0.4))
    assert box_size((0.2, 0.4, 0.6, 0.8)) == pytest.approx((0.4, 0.4))


def test_head_region_is_the_top_forty_percent_of_a_person_box():
    assert head_region((0.0, 0.2, 1.0, 0.4)) == pytest.approx((0.0, 0.2, 0.4, 0.4))


def test_feet_visible_is_false_when_the_box_touches_the_frame_bottom():
    assert feet_visible((0.1, 0.2, 0.9, 0.4))
    assert not feet_visible((0.1, 0.2, 1.0, 0.4))


def test_floor_range_recovers_the_range_the_box_was_built_from():
    model = CameraModel.published_default()
    for expected in (1.0, 1.5, 2.0, 3.0):
        assert model.floor_range(person_box(expected), 0.0) == pytest.approx(expected, rel=1e-3)


def test_floor_range_is_capped_where_the_ray_starts_grazing():
    model = CameraModel.published_default()
    assert model.floor_range(person_box(6.0), 0.0) == pytest.approx(MAX_FLOOR_RANGE_M)


def test_floor_range_is_none_above_the_horizon():
    model = CameraModel.published_default()
    assert model.floor_range((0.05, 0.4, 0.2, 0.6), 0.0) is None


def test_floor_range_is_none_when_the_feet_are_cut_off_by_the_frame():
    model = CameraModel.published_default()
    assert model.floor_range((0.2, 0.4, 1.0, 0.6), 0.0) is None


def test_height_from_range_recovers_a_standing_adult():
    model = CameraModel.published_default()
    box = person_box(2.0, height_m=1.70)
    assert model.height_from_range(box, 2.0, 0.0) == pytest.approx(1.70, rel=1e-3)


def test_height_from_range_reads_a_child_shorter_than_an_adult():
    model = CameraModel.published_default()
    child = model.height_from_range(person_box(2.0, height_m=1.20), 2.0, 0.0)
    assert child is not None and child == pytest.approx(1.20, rel=1e-3)


def test_range_from_height_is_the_fallback_when_the_feet_are_out_of_frame():
    model = CameraModel.published_default()
    box = person_box(2.0)
    assert model.range_from_height(box, 1.70, 0.0) == pytest.approx(2.0, rel=1e-3)


def test_bearing_is_positive_to_the_left_of_the_robot():
    model = CameraModel.published_default()
    assert model.bearing_deg((0.4, 0.05, 0.8, 0.25)) > 0
    assert model.bearing_deg((0.4, 0.75, 0.8, 0.95)) < 0


def test_bearing_of_a_box_on_the_principal_point_is_zero():
    model = CameraModel.published_default()
    assert model.bearing_deg((0.4, CENTER_X - 0.05, 0.8, CENTER_X + 0.05)) == pytest.approx(0.0)


def test_head_pitch_tilted_down_shortens_the_range_of_the_same_pixel():
    model = CameraModel.published_default()
    box = person_box(2.0)
    tilted = model.floor_range(box, -15.0)
    assert tilted is not None and tilted < 2.0


def test_head_pitch_tilted_up_puts_the_feet_pixel_above_the_horizon():
    model = CameraModel.published_default()
    assert model.floor_range(person_box(2.0), 15.0) is None


def test_elevation_of_a_box_above_the_centre_is_positive():
    model = CameraModel.published_default()
    assert model.elevation_of((0.05, 0.4, 0.15, 0.6)) > 0
    assert model.elevation_of((0.85, 0.4, 0.95, 0.6)) < 0


def test_box_pixels_and_height_are_in_the_model_own_resolution():
    native = CameraModel.published_default().native()
    assert native.box_pixels((0.0, 0.0, 0.5, 0.5)) == (0, 0, 640, 360)
    assert native.box_height_px((0.1, 0.0, 0.6, 0.5)) == pytest.approx(360.0)


# --------------------------------------------------------------- face pose


def _landmarks(*, nose_dx: float = 0.0, nose_along: float = 0.5) -> tuple:
    eye_span, face_span = 20.0, 30.0
    left_eye = (50.0 - eye_span / 2, 40.0)
    right_eye = (50.0 + eye_span / 2, 40.0)
    nose = (50.0 + nose_dx, 40.0 + face_span * nose_along)
    return (left_eye, right_eye, nose, (44.0, 70.0), (56.0, 70.0))


def test_pose_from_landmarks_reads_a_centred_face_as_frontal():
    assert pose_from_landmarks(_landmarks()) == pytest.approx((0.0, 0.0))


def test_pose_from_landmarks_reads_a_nose_off_centre_as_yaw():
    yaw, _ = pose_from_landmarks(_landmarks(nose_dx=10.0))
    assert yaw == pytest.approx(45.0)


def test_pose_from_landmarks_reads_a_nose_low_between_eyes_and_mouth_as_seen_from_below():
    _, pitch = pose_from_landmarks(_landmarks(nose_along=0.7))
    assert pitch > 0


def test_pose_from_landmarks_reads_a_nose_high_as_seen_from_above():
    _, pitch = pose_from_landmarks(_landmarks(nose_along=0.3))
    assert pitch < 0


def test_pose_from_landmarks_without_landmarks_reads_frontal():
    assert pose_from_landmarks(()) == (0.0, 0.0)


def test_pose_from_landmarks_is_bounded():
    yaw, pitch = pose_from_landmarks(_landmarks(nose_dx=500.0, nose_along=50.0))
    assert abs(yaw) <= 90.0 and abs(pitch) <= 90.0


def test_ray_of_the_principal_point_is_the_optical_axis():
    model = CameraModel.published_default()
    dx, dy, dz = model.ray(model.cx, model.cy, 0.0)
    assert (dx, dy, dz) == pytest.approx((1.0, 0.0, 0.0))


def test_ray_pitches_with_the_head():
    model = CameraModel.published_default()
    dx, _dy, dz = model.ray(model.cx, model.cy, 20.0)
    assert math.degrees(math.atan2(dz, dx)) == pytest.approx(20.0)
