"""Fisheye -> pinhole remap. Pure geometry: no hardware, no OpenCV, no ROS."""

from __future__ import annotations

import math

import numpy as np
import pytest
from innate_nav.rectify import DoubleSphere, Pinhole, build_maps, coverage

# innate-nav-data's `mars` front_fish preset.
XI, ALPHA = -0.27, 0.57


def _fisheye(fx=250.0, w=640, h=480, xi=XI, alpha=ALPHA) -> DoubleSphere:
    return DoubleSphere(fx=fx, fy=fx, cx=w / 2, cy=h / 2, xi=xi, alpha=alpha, width=w, height=h)


def _policy_view() -> Pinhole:
    return Pinhole.from_hfov(110.0, 640, 480)


# --- the virtual camera ------------------------------------------------------

def test_policy_view_has_the_focal_length_training_used():
    p = _policy_view()
    assert p.fx == pytest.approx(224.07, abs=0.01)
    assert (p.cx, p.cy) == (320.0, 240.0)


def test_camera_info_k_is_row_major_with_the_focal_length_on_the_diagonal():
    k = _policy_view().k
    assert k[0] == pytest.approx(224.07, abs=0.01)   # fx
    assert k[4] == pytest.approx(224.07, abs=0.01)   # fy
    assert (k[2], k[5]) == (320.0, 240.0)            # cx, cy
    assert k[8] == 1.0


def test_rays_are_unit_and_the_centre_ray_points_straight_ahead():
    p = _policy_view()
    r = p.rays()
    assert np.allclose(np.linalg.norm(r, axis=1), 1.0)
    centre = r.reshape(p.height, p.width, 3)[p.height // 2, p.width // 2]
    assert centre[2] > 0.99


# --- the lens model ----------------------------------------------------------

def test_a_zero_distortion_double_sphere_is_exactly_a_pinhole():
    """xi=0, alpha=0 collapses the model to u = fx*x/z + cx. If our projection
    does not reproduce that, the algebra is wrong somewhere subtler."""
    dst = _policy_view()
    lens = _fisheye(fx=dst.fx, xi=0.0, alpha=0.0)  # exact, not rounded: this is an identity test
    map_x, map_y, valid = build_maps(lens, dst)
    assert valid.all()
    ys, xs = np.mgrid[0 : dst.height, 0 : dst.width]
    assert np.allclose(map_x, xs, atol=1e-3)
    assert np.allclose(map_y, ys, atol=1e-3)


def test_project_and_unproject_round_trip():
    lens = _fisheye()
    rays = _policy_view().rays()
    u, v, ok = lens.project(rays)
    back = lens.unproject(u[ok], v[ok])
    assert np.allclose(back, rays[ok], atol=1e-6)


def test_the_image_grows_monotonically_with_angle_off_axis():
    lens = _fisheye()
    radii = []
    for deg in (0, 10, 20, 30, 40, 50):
        a = math.radians(deg)
        u, v, ok = lens.project(np.array([[math.sin(a), 0.0, math.cos(a)]]))
        assert ok[0]
        radii.append(float(u[0] - lens.cx))
    assert all(b > a for a, b in zip(radii, radii[1:], strict=False))


def test_a_fisheye_compresses_the_periphery_relative_to_a_pinhole():
    """The reason a 150 deg lens fits on a sensor at all: off-axis rays land
    closer to the centre than tan(theta) would put them."""
    lens = _fisheye()
    a = math.radians(50.0)
    u, _v, ok = lens.project(np.array([[math.sin(a), 0.0, math.cos(a)]]))
    assert ok[0]
    assert float(u[0] - lens.cx) < lens.fx * math.tan(a)


def test_rays_behind_the_lens_horizon_are_marked_invalid_not_aliased():
    lens = _fisheye()
    _u, _v, ok = lens.project(np.array([[0.0, 0.0, -1.0]]))
    assert not ok[0]


# --- the remap ---------------------------------------------------------------

def test_maps_have_the_output_shape_and_float32_for_cv2():
    dst = _policy_view()
    map_x, map_y, _ = build_maps(_fisheye(), dst)
    assert map_x.shape == map_y.shape == (dst.height, dst.width)
    assert map_x.dtype == map_y.dtype == np.float32


def test_the_output_centre_samples_the_source_centre():
    lens, dst = _fisheye(), _policy_view()
    map_x, map_y, _ = build_maps(lens, dst)
    assert map_x[dst.height // 2, dst.width // 2] == pytest.approx(lens.cx, abs=0.5)
    assert map_y[dst.height // 2, dst.width // 2] == pytest.approx(lens.cy, abs=0.5)


def test_unreachable_pixels_point_off_image_so_remap_blanks_them():
    """A lens too narrow for the request must leave holes, not smear the edge
    texel across the corners — that would be invented image content."""
    narrow = _fisheye(fx=1200.0)
    map_x, map_y, valid = build_maps(narrow, _policy_view())
    assert not valid.all()
    assert (map_x[~valid] < 0).all() and (map_y[~valid] < 0).all()


# --- coverage: can the lens even reach 110 deg? ------------------------------

def test_a_wide_lens_covers_the_whole_policy_view():
    assert coverage(_fisheye(fx=200.0), _policy_view()) == 1.0


def test_full_coverage_needs_a_very_short_source_focal_length():
    """The 110 deg view wants rays 60.7 deg off-axis (a 121.5 deg DIAGONAL
    field), and on a 4:3 sensor the vertical edge runs out before the corner
    does. With this lens shape the whole view is only reachable while the
    source fx stays under ~215px. Upstream's back-solved head-camera estimate
    is fx ~= 355, so 110 deg is NOT extractable under those numbers -- which is
    what coverage() exists to say out loud, against a real calibration."""
    dst = _policy_view()
    assert coverage(_fisheye(fx=210.0), dst) == 1.0
    assert coverage(_fisheye(fx=220.0), dst) < 1.0


def test_a_narrow_lens_is_reported_as_partial_coverage():
    c = coverage(_fisheye(fx=1200.0), _policy_view())
    assert 0.0 < c < 1.0


def test_coverage_is_monotonic_in_focal_length():
    dst = _policy_view()
    cs = [coverage(_fisheye(fx=f), dst) for f in (250.0, 500.0, 800.0, 1200.0)]
    assert all(b <= a for a, b in zip(cs, cs[1:], strict=False))
