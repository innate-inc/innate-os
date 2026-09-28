# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Tests for the ground-plane geometry behind the depth obstacle corridor.

Synthetic floors with a known tilt, so a sign flip or a broken fit shows up as a
number rather than as phantom obstacles on the robot. No ROS here.
"""

from pathlib import Path

import numpy as np
import pytest

from mars_cam.ground_plane import (
    Corridor,
    Intrinsics,
    fit_floor,
    height_error_by_range,
    image_radius,
    leak_fractions,
    mount_correction_for,
    optical_mount_rotation,
    quaternion_matrix,
    transform_to_base,
)


def floor_points(pitch_deg: float = 0.0, roll_deg: float = 0.0, offset_m: float = 0.0, noise_m: float = 0.0):
    """A floor spanning 0.2-2.5 m ahead, tilted by a known amount."""
    rng = np.random.default_rng(3)
    x = rng.uniform(0.2, 2.5, 4000)
    y = rng.uniform(-0.8, 0.8, 4000)
    z = np.tan(np.radians(pitch_deg)) * x + np.tan(np.radians(roll_deg)) * y + offset_m
    if noise_m:
        z = z + rng.normal(0.0, noise_m, len(z))
    return np.column_stack([x, y, z])


# ------------------------------------------------------------------ plane fit


def test_flat_floor_reads_as_flat():
    fit = fit_floor(floor_points())

    assert fit is not None
    assert fit.pitch_deg == pytest.approx(0.0, abs=1e-6)
    assert fit.roll_deg == pytest.approx(0.0, abs=1e-6)
    assert fit.offset_m == pytest.approx(0.0, abs=1e-6)


def test_recovers_a_known_pitch():
    fit = fit_floor(floor_points(pitch_deg=1.5))

    assert fit is not None
    assert fit.pitch_deg == pytest.approx(1.5, abs=0.01)
    # A positive pitch must read as the floor rising with distance ahead.
    assert fit.height_at(1.2) > 0.0
    assert fit.height_at(1.2) == pytest.approx(1.2 * np.tan(np.radians(1.5)), abs=1e-4)


def test_recovers_pitch_roll_and_offset_together():
    fit = fit_floor(floor_points(pitch_deg=-0.8, roll_deg=0.4, offset_m=0.01))

    assert fit is not None
    assert fit.pitch_deg == pytest.approx(-0.8, abs=0.01)
    assert fit.roll_deg == pytest.approx(0.4, abs=0.01)
    assert fit.offset_m == pytest.approx(0.01, abs=1e-4)


def test_pitch_error_at_range_matches_the_budget_table():
    """The plan's error budget must hold: 2 degrees is ~42mm at 1.2m."""
    fit = fit_floor(floor_points(pitch_deg=2.0))

    assert fit is not None
    assert fit.height_at(1.2) * 1000 == pytest.approx(42.0, abs=1.0)


def test_an_obstacle_does_not_tilt_the_floor_fit():
    """A box in view must not drag the plane off the floor."""
    floor = floor_points(pitch_deg=0.5)
    box = np.column_stack([np.full(600, 1.0), np.linspace(-0.1, 0.1, 600), np.full(600, 0.30)])
    fit = fit_floor(np.vstack([floor, box]))

    assert fit is not None
    assert fit.pitch_deg == pytest.approx(0.5, abs=0.05)


def test_returns_none_without_enough_floor():
    assert fit_floor(np.array([[1.0, 0.0, 0.9]])) is None


def test_noise_lands_in_the_residual_not_the_pitch():
    fit = fit_floor(floor_points(pitch_deg=1.0, noise_m=0.005))

    assert fit is not None
    assert fit.pitch_deg == pytest.approx(1.0, abs=0.05)
    assert fit.residual.rms == pytest.approx(5.0, abs=1.5)  # mm


# ----------------------------------------------------------------- range bins


def test_height_error_grows_with_range_under_a_pitch_error():
    rows = [r for r in height_error_by_range(floor_points(pitch_deg=1.0)) if r[2].count]
    means = [stats.mean for _, _, stats in rows]

    assert means == sorted(means), "a pitch error must show as a monotonic trend across range"
    assert means[-1] > means[0]


def test_flat_floor_shows_no_range_trend():
    rows = [r for r in height_error_by_range(floor_points()) if r[2].count]
    assert all(abs(stats.mean) < 1.0 for _, _, stats in rows)  # mm


# ------------------------------------------------------------------- leakage


def test_leak_fraction_rises_as_the_threshold_falls():
    fractions = leak_fractions(floor_points(pitch_deg=1.5))
    assert fractions[0.015] > fractions[0.025] > fractions[0.050]


def test_flat_floor_leaks_nothing():
    assert leak_fractions(floor_points())[0.015] == pytest.approx(0.0)


# ------------------------------------------------------------------ corridor


def test_corridor_keeps_only_what_is_ahead_and_in_width():
    corridor = Corridor()
    points = np.array(
        [
            [0.8, 0.0, 0.10],  # in
            [0.8, 0.50, 0.10],  # too far left
            [2.0, 0.0, 0.10],  # beyond the horizon
            [0.10, 0.0, 0.10],  # behind the front edge
            [0.8, 0.0, 0.012],  # rollable at 12mm, below the 15mm line
            [0.8, 0.0, 0.50],  # above z_max
        ]
    )
    assert corridor.mask(points).tolist() == [True, False, False, False, False, False]


def test_footprint_mask_ignores_height():
    corridor = Corridor()
    points = np.array([[0.8, 0.0, 0.012], [0.8, 0.0, 2.0], [3.0, 0.0, 0.1]])
    assert corridor.footprint_mask(points).tolist() == [True, True, False]


def test_a_short_corridor_tolerates_more_pitch_error():
    """The core argument for the corridor: halving the horizon halves the error."""
    tilted = floor_points(pitch_deg=2.0)
    near = Corridor(x_max=1.2).footprint_mask(tilted)
    far = Corridor(x_max=2.5).footprint_mask(tilted)

    assert tilted[near][:, 2].max() < tilted[far][:, 2].max()
    assert tilted[near][:, 2].max() < 0.05  # stays well under the far-range error


# ------------------------------------------------------------------ transform


def test_identity_transform_is_a_no_op():
    points = floor_points()
    moved = transform_to_base(points, np.eye(3), np.zeros(3))
    assert np.allclose(moved, points)


def test_transform_applies_rotation_then_translation():
    points = np.array([[1.0, 0.0, 0.0]])
    # 90 degrees about z: x forward becomes y left.
    rotation = quaternion_matrix(0.0, 0.0, np.sin(np.pi / 4), np.cos(np.pi / 4))
    moved = transform_to_base(points, rotation, np.array([0.0, 0.0, 0.25]))
    assert moved[0] == pytest.approx([0.0, 1.0, 0.25], abs=1e-9)


def test_quaternion_matrix_is_orthonormal():
    matrix = quaternion_matrix(0.1, -0.3, 0.2, 0.9)
    assert np.allclose(matrix @ matrix.T, np.eye(3), atol=1e-12)
    assert np.linalg.det(matrix) == pytest.approx(1.0)


def test_degenerate_quaternion_falls_back_to_identity():
    assert np.allclose(quaternion_matrix(0.0, 0.0, 0.0, 0.0), np.eye(3))


def test_camera_pitch_shows_up_as_floor_pitch_after_transform():
    """A camera pitched down by 1 degree tilts the reconstructed floor by 1 degree.

    This is the whole mechanism behind floor leakage, pinned end to end.
    """
    flat = floor_points()
    tilt = np.radians(1.0)
    # Rotation about +y tilts the forward axis into the vertical one.
    pitch = np.array([[np.cos(tilt), 0.0, np.sin(tilt)], [0.0, 1.0, 0.0], [-np.sin(tilt), 0.0, np.cos(tilt)]])
    fit = fit_floor(transform_to_base(flat, pitch, np.zeros(3)))

    assert fit is not None
    assert abs(fit.pitch_deg) == pytest.approx(1.0, abs=0.02)


# ------------------------------------------------------------ image position


def _forward_camera(height: float = 0.248, pitch_deg: float = -20.0):
    """Camera->base rotation/translation for a head pitched down by `pitch_deg`.

    Optical convention (x right, y down, z forward) mapped into base_link
    (x forward, y left, z up), then pitched about the base y axis.
    """
    optical_to_base = np.array([[0.0, 0.0, 1.0], [-1.0, 0.0, 0.0], [0.0, -1.0, 0.0]])
    # Negated: a negative head angle is nose-down, which is a positive rotation
    # about base +y.
    t = np.radians(-pitch_deg)
    pitch = np.array([[np.cos(t), 0.0, np.sin(t)], [0.0, 1.0, 0.0], [-np.sin(t), 0.0, np.cos(t)]])
    return pitch @ optical_to_base, np.array([0.0, 0.0, height])


INTRINSICS = Intrinsics(fx=275.84, fy=275.84, cx=320.0, cy=240.0, width=640, height=480)


def test_ground_ahead_projects_below_the_horizon():
    rotation, translation = _forward_camera(pitch_deg=0.0)
    radii = image_radius(np.array([[1.0, 0.0, 0.0]]), rotation, translation, INTRINSICS)

    assert radii.size == 1
    assert 0.0 < radii[0] < 0.5


def test_tilting_down_brings_the_one_metre_spot_toward_centre():
    """The reason for the -20 degree head position, pinned as a number."""
    spot = np.array([[1.0, 0.0, 0.0]])
    level = image_radius(spot, *_forward_camera(pitch_deg=0.0), INTRINSICS)[0]
    tilted = image_radius(spot, *_forward_camera(pitch_deg=-14.0), INTRINSICS)[0]

    assert tilted < level
    assert tilted < 0.02  # -14 deg centres it


def test_whole_corridor_stays_off_the_lens_edge_at_the_ai_head_position():
    corridor = Corridor()
    x = np.linspace(corridor.x_min, corridor.x_max, 40)
    ground = np.column_stack([x, np.zeros_like(x), np.zeros_like(x)])
    radii = image_radius(ground, *_forward_camera(pitch_deg=-20.0), INTRINSICS)

    assert radii.max() < 0.4, "corridor must not reach the outer ring where plumb_bob stops fitting"


def test_points_behind_the_camera_are_dropped():
    rotation, translation = _forward_camera(pitch_deg=0.0)
    assert image_radius(np.array([[-1.0, 0.0, 0.0]]), rotation, translation, INTRINSICS).size == 0


# ------------------------------------------------------- mount correction


def _tilted_cloud(error_deg: float, head_deg: float = -20.0):
    """A truly flat floor, mis-rotated in the optical frame by `error_deg`."""
    rotation, translation = _forward_camera(pitch_deg=head_deg)
    x = np.linspace(0.25, 1.0, 200)
    flat_base = np.column_stack([x, np.zeros_like(x), np.zeros_like(x)])
    optical = (flat_base - translation) @ rotation
    corrupted = optical @ optical_mount_rotation(error_deg, 0.0).T
    return corrupted, rotation, translation


def test_an_optical_tilt_reads_as_an_equal_floor_slope():
    """The mechanism: mount error in the optical frame == floor pitch in base."""
    for error in (-3.0, -1.5, 1.5, 3.168):
        corrupted, rotation, translation = _tilted_cloud(error)
        fit = fit_floor(transform_to_base(corrupted, rotation, translation), band_m=0.5)
        assert fit is not None
        assert fit.pitch_deg == pytest.approx(error, abs=1e-3)


def test_the_recommended_correction_flattens_the_floor():
    """Pins the sign the diagnostic prints — a flip here would double the error."""
    for error in (-3.0, 1.5, 3.168):
        corrupted, rotation, translation = _tilted_cloud(error)
        fit = fit_floor(transform_to_base(corrupted, rotation, translation), band_m=0.5)
        assert fit is not None

        pitch_fix, roll_fix = mount_correction_for(fit)
        fixed = corrupted @ optical_mount_rotation(pitch_fix, roll_fix).T
        after = fit_floor(transform_to_base(fixed, rotation, translation), band_m=0.5)

        assert after is not None
        assert after.pitch_deg == pytest.approx(0.0, abs=1e-3)


def test_mount_rotation_is_orthonormal():
    matrix = optical_mount_rotation(-3.168, 0.5)
    assert np.allclose(matrix @ matrix.T, np.eye(3), atol=1e-12)
    assert np.linalg.det(matrix) == pytest.approx(1.0)


def test_zero_correction_is_identity():
    assert np.allclose(optical_mount_rotation(0.0, 0.0), np.eye(3))


# ------------------------------------------------------------ vibration


def test_per_frame_fits_expose_shake_the_pooled_fit_hides():
    """Vibration is zero-mean, so pooling averages it away.

    STVL latches a mark for voxel_decay seconds, so the worst single frame is
    what reaches the costmap. Per-frame fits are the only way to see it.
    """
    rng = np.random.default_rng(11)
    shake_deg = rng.normal(0.0, 0.5, 12)
    frames = [floor_points(pitch_deg=float(s)) for s in shake_deg]

    pooled = fit_floor(np.vstack(frames))
    per_frame = np.array([fit_floor(f).pitch_deg for f in frames])

    assert pooled is not None
    assert abs(pooled.pitch_deg) < 0.2, "pooling hides the shake"
    assert per_frame.std() == pytest.approx(0.5, abs=0.2), "per-frame fits recover the amplitude"
    assert np.abs(per_frame).max() > 3 * abs(pooled.pitch_deg)


def test_a_steady_tilt_shows_no_frame_to_frame_spread():
    """A systematic mount error must not be mistaken for vibration."""
    frames = [floor_points(pitch_deg=3.168) for _ in range(8)]
    per_frame = np.array([fit_floor(f).pitch_deg for f in frames])

    assert per_frame.mean() == pytest.approx(3.168, abs=0.01)
    assert per_frame.std() < 1e-6


# ------------------------------------------------------- mount persistence


def test_mount_correction_round_trips_through_the_file():
    import tempfile

    from mars_cam import camera_mount

    with tempfile.TemporaryDirectory() as d:
        path = camera_mount.mount_path(Path(d))
        original = camera_mount.MountCorrection(
            pitch_deg=-3.420, roll_deg=0.398, height_m=-0.0143, head_angle_deg=-20.04, residual_rms_mm=1.5
        )
        camera_mount.save(path, original)
        loaded = camera_mount.load(path)

    assert loaded is not None
    assert loaded.pitch_deg == pytest.approx(-3.420)
    assert loaded.roll_deg == pytest.approx(0.398)
    assert loaded.height_m == pytest.approx(-0.0143)
    assert loaded.head_angle_deg == pytest.approx(-20.04)
    assert loaded.measured_at != "", "a saved correction must record when it was measured"


def test_missing_mount_file_is_not_an_error():
    from mars_cam import camera_mount

    assert camera_mount.load(Path("/nonexistent/camera_mount.yaml")) is None


def test_corrections_accumulate_as_absolute_values():
    """The diagnostic measures a residual; the file stores the absolute total."""
    from mars_cam import camera_mount

    current = camera_mount.MountCorrection(pitch_deg=-3.126, roll_deg=0.398, height_m=0.0)
    # A later run measures +0.294 deg of residual tilt and +14.3mm of height.
    updated = current.plus(-0.294, 0.0, -0.0143)

    assert updated.pitch_deg == pytest.approx(-3.420)
    assert updated.roll_deg == pytest.approx(0.398)
    assert updated.height_m == pytest.approx(-0.0143)


def test_calibration_dir_discovery_matches_the_cpp_rule():
    import tempfile

    from mars_cam import camera_mount

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "maps").mkdir()
        (root / "mars_v2_calibration_config").mkdir()
        found = camera_mount.find_calibration_dir(root)

    assert found is not None
    assert "calibration_config" in found.name


def test_no_calibration_dir_returns_none():
    import tempfile

    from mars_cam import camera_mount

    with tempfile.TemporaryDirectory() as d:
        assert camera_mount.find_calibration_dir(Path(d)) is None
