"""Pinhole -> pinhole resampling. Pure geometry: no ROS, no OpenCV, no camera.

The numbers here are the real MARS calibration (stereo_calib P1: fx 420.07,
cx 625.55, cy 356.92 over 1280x720), so these tests say what that camera can
and cannot give the policy.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from innate_nav.wide_view_maps import Pinhole, coverage, resample_maps

# /mars/main_camera/left/image_rect as this robot publishes it.
MARS_RECT = Pinhole(fx=420.0742, fy=420.0742, cx=625.5523, cy=356.9170, width=1280, height=720)
# What the checkpoint was trained on, trimmed for a 16:9 sensor with margin.
POLICY = Pinhole.from_hfov(110.0, 640, 370)


def test_the_policy_view_has_the_focal_length_training_used():
    assert POLICY.fx == pytest.approx(224.07, abs=0.01)
    assert (POLICY.cx, POLICY.cy) == (320.0, 185.0)


def test_identical_cameras_map_to_themselves():
    """The degenerate case: if source and destination are the same pinhole the
    map must be the identity, or the affine has a sign or offset error."""
    map_x, map_y, valid = resample_maps(POLICY, POLICY)
    assert valid.all()
    ys, xs = np.mgrid[0 : POLICY.height, 0 : POLICY.width]
    assert np.allclose(map_x, xs, atol=1e-3)
    assert np.allclose(map_y, ys, atol=1e-3)


def test_the_centre_of_the_view_samples_the_source_principal_point():
    map_x, map_y, _ = resample_maps(MARS_RECT, POLICY)
    assert map_x[POLICY.height // 2, POLICY.width // 2] == pytest.approx(MARS_RECT.cx, abs=1.0)
    assert map_y[POLICY.height // 2, POLICY.width // 2] == pytest.approx(MARS_RECT.cy, abs=1.0)


def test_a_pixel_maps_to_the_source_pixel_at_the_same_angle():
    """The invariant the whole thing rests on: both cameras put a ray at angle
    theta at f*tan(theta) from the centre, so the mapping preserves angle."""
    map_x, map_y, _ = resample_maps(MARS_RECT, POLICY)
    col = 600
    theta = math.atan((col - POLICY.cx) / POLICY.fx)
    expected = MARS_RECT.cx + MARS_RECT.fx * math.tan(theta)
    assert map_x[POLICY.height // 2, col] == pytest.approx(expected, abs=1e-3)


def test_the_real_camera_covers_the_trimmed_policy_view():
    assert coverage(MARS_RECT, POLICY) == 1.0


def test_380_rows_would_fit_this_unit_but_with_no_margin_at_all():
    """Why the view is 370 and not the 380 that fits: 380 leaves under a pixel
    of principal-point spread, so a sister robot would blank rows."""
    assert coverage(MARS_RECT, Pinhole.from_hfov(110.0, 640, 380)) == 1.0
    shifted = Pinhole(fx=MARS_RECT.fx, fy=MARS_RECT.fy, cx=MARS_RECT.cx,
                      cy=MARS_RECT.cy - 3.0, width=1280, height=720)
    assert coverage(shifted, Pinhole.from_hfov(110.0, 640, 380)) < 1.0
    assert coverage(shifted, POLICY) == 1.0


def test_the_real_camera_cannot_cover_the_untrimmed_480_row_view():
    """Why the view is 381 rows and not 480: a 4:3 frame at 110 deg needs 93.9
    deg vertically and this 16:9 sensor has ~81. Asking for 480 blanks the top
    and bottom rather than failing loudly, so the node checks coverage."""
    untrimmed = Pinhole.from_hfov(110.0, 640, 480)
    c = coverage(MARS_RECT, untrimmed)
    assert c < 1.0
    assert 0.75 < c < 0.85          # ~44 blank rows top and bottom


def test_uncovered_pixels_point_off_image_so_remap_blanks_them():
    untrimmed = Pinhole.from_hfov(110.0, 640, 480)
    map_x, map_y, valid = resample_maps(MARS_RECT, untrimmed)
    assert not valid.all()
    assert (map_x[~valid] < 0).all() and (map_y[~valid] < 0).all()


def test_widening_beyond_the_source_is_reported_not_invented():
    """A ray the source never captured cannot be resampled into existence."""
    too_wide = Pinhole.from_hfov(150.0, 640, 381)
    assert coverage(MARS_RECT, too_wide) < 1.0


def test_half_angles_account_for_an_off_centre_principal_point():
    """cy=356.92 of 720 sits above centre, so this camera reaches further down
    than up; the binding limit is the smaller side."""
    h, v = MARS_RECT.half_angles_deg()
    assert h == pytest.approx(56.12, abs=0.05)
    assert v == pytest.approx(40.35, abs=0.05)


def test_a_slightly_different_unit_still_covers_the_view():
    """Per-robot calibration spread: this is why the node reads P off the wire
    instead of shipping one robot's numbers as a constant."""
    for dfx, dcx, dcy in ((-8.0, -6.0, -5.0), (8.0, 6.0, 5.0), (0.0, 12.0, -9.0), (4.0, -9.0, 9.0)):
        unit = Pinhole(fx=MARS_RECT.fx + dfx, fy=MARS_RECT.fy + dfx,
                       cx=MARS_RECT.cx + dcx, cy=MARS_RECT.cy + dcy,
                       width=1280, height=720)
        assert coverage(unit, POLICY) == 1.0, (dfx, dcx, dcy)
