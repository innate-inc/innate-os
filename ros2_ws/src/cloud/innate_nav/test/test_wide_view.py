"""The policy's virtual camera and the map that fills it from the raw image.

Numbers are a real MARS left camera as its camera_info reports it, so these
tests say what that sensor can and cannot give the policy.
"""

from __future__ import annotations

import math

import pytest
from innate_nav.wide_view_maps import Lens, Pinhole, coverage, undistort_maps

# /mars/main_camera/left/camera_info from a real robot. fx != fy because the
# driver squashes 1280x720 into 640x480, so the pixels are not square.
MARS_K = [198.0389, 0.0, 326.8270, 0.0, 264.6403, 229.3660, 0.0, 0.0, 1.0]
MARS_D = [0.0128943, -0.0305921, -0.000264048, -0.000176999, 0.00519322]
MARS = Lens.from_camera_info(MARS_K, MARS_D, 640, 480)

POLICY = Pinhole.from_hfov(110.0, 640, 370)


def test_the_policy_view_has_the_focal_length_training_used():
    assert POLICY.fx == pytest.approx(224.07, abs=0.01)
    assert POLICY.fx == POLICY.fy          # training rendered square pixels
    assert (POLICY.cx, POLICY.cy) == (320.0, 185.0)


def test_the_sensor_is_wider_than_its_pinhole_approximation_suggests():
    """A pinhole formula on K understates a distorted lens; the table that said
    115.9 deg was computed that way. Traced through D it is ~122."""
    h, v = MARS.half_angles_deg()
    naive_h = math.degrees(math.atan(MARS.k[0, 2] / MARS.k[0, 0]))
    assert h > naive_h
    assert 2 * h == pytest.approx(121.8, abs=1.5)
    assert 2 * v == pytest.approx(82.1, abs=1.5)


def test_the_raw_sensor_covers_the_policy_view():
    """The whole reason this reads image_raw and not image_rect."""
    assert coverage(MARS, POLICY) == 1.0


def test_the_stereo_rectified_stream_would_not_have_covered_it():
    """image_rect is rectified with alpha=0 -- cropped to pixels valid in both
    cameras -- leaving ~100 deg where the policy needs 110. Modelled here as
    the distortion-free pinhole it is, at the P the robot publishes."""
    rect = Lens.from_camera_info(
        [266.4048, 0.0, 324.1737, 0.0, 266.4048, 226.2469, 0.0, 0.0, 1.0], [0.0] * 5, 640, 480)
    c = coverage(rect, POLICY)
    assert c < 1.0
    assert 0.80 < c < 0.88          # the 83.9% the robot logged


def test_the_untrimmed_480_row_view_does_not_fit_even_from_raw():
    """Why the view is 370 rows: 4:3 at 110 deg wants 93.9 deg vertically and
    the sensor has ~82, whatever we rectify from."""
    assert coverage(MARS, Pinhole.from_hfov(110.0, 640, 480)) < 1.0


def test_uncovered_pixels_point_off_image_so_remap_blanks_them():
    map_x, map_y, valid = undistort_maps(MARS, Pinhole.from_hfov(150.0, 640, 370))
    assert not valid.all()
    assert (map_x[~valid] < 0).all() and (map_y[~valid] < 0).all()


def test_the_view_centre_samples_the_lens_principal_point():
    map_x, map_y, _ = undistort_maps(MARS, POLICY)
    assert map_x[POLICY.height // 2, POLICY.width // 2] == pytest.approx(MARS.k[0, 2], abs=1.5)
    assert map_y[POLICY.height // 2, POLICY.width // 2] == pytest.approx(MARS.k[1, 2], abs=1.5)


def test_a_lens_scaled_to_another_resolution_keeps_its_field_of_view():
    """The driver may publish at a size it was not calibrated at. D acts on
    normalised coordinates, so only K scales -- and the angles must not move."""
    big = MARS.scaled_to(1280, 960)
    # 0.1 deg, not 0: the edge sample sits at w-1, so a finer grid reaches a
    # hair further out. That is discretisation, not a change of field.
    assert big.half_angles_deg() == pytest.approx(MARS.half_angles_deg(), abs=0.1)
    assert coverage(big, POLICY) == 1.0


def test_scaling_to_the_same_size_is_a_no_op():
    assert MARS.scaled_to(640, 480) is MARS


def test_nominal_parameters_still_cover_the_view_on_an_uncalibrated_robot():
    """Units ship the same camera, so a robot with no calibration file uses
    nominal values; per-unit spread must not push it below full coverage."""
    for dfx, dcx, dcy in ((-6.0, -8.0, -6.0), (6.0, 8.0, 6.0), (0.0, 10.0, -10.0)):
        assert coverage(_shifted(dfx, dcx, dcy), POLICY) == 1.0, (dfx, dcx, dcy)


def test_a_unit_far_enough_off_nominal_is_reported_rather_than_silently_blanked():
    """The nominal lens has roughly +/-12px of principal-point margin at 370
    rows. Past that the view starts to blank, and the node says so instead of
    publishing a black edge that looks like a dark obstacle."""
    assert coverage(_shifted(0.0, 14.0, -14.0), POLICY) < 1.0


def _shifted(dfx: float, dcx: float, dcy: float) -> Lens:
    k = list(MARS_K)
    k[0] += dfx
    k[4] += dfx
    k[2] += dcx
    k[5] += dcy
    return Lens.from_camera_info(k, MARS_D, 640, 480)
