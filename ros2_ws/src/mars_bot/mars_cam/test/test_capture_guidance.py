# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Tests for the live capture guidance.

The behaviour under test is the difference between an operator who is told
"73%, still need the top-left corner and more tilt" and one who is told "27 of
40" while filling the middle of the frame with square-on views that cannot
constrain the calibration.
"""

import numpy as np
import pytest

from mars_cam.capture_guidance import (
    DEFAULT_EXTENT_MIN,
    EXTENT_MAX,
    IN_RANGE,
    TILT_MIN,
    TOO_CLOSE,
    TOO_FAR,
    BoardGeometry,
    BoardView,
    CoverageTracker,
    measure,
)

EXTENT_MIN = DEFAULT_EXTENT_MIN

FRAME = (640, 480)
FOCAL = 500.0


def board_points(cols: int = 9, rows: int = 6, pitch: float = 0.022) -> np.ndarray:
    """Inner corners of a checkerboard, in board coordinates, z = 0."""
    grid = np.zeros((rows * cols, 3), dtype=np.float64)
    grid[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * pitch
    return grid


def project(
    points: np.ndarray,
    distance: float,
    pitch_deg: float = 0.0,
    yaw_deg: float = 0.0,
    offset: tuple[float, float] = (0.0, 0.0),
) -> np.ndarray:
    """Image points for a board centred on the optical axis, then rotated."""
    centred = points - points.mean(axis=0)
    p, y = np.radians(pitch_deg), np.radians(yaw_deg)
    rot_x = np.array([[1, 0, 0], [0, np.cos(p), -np.sin(p)], [0, np.sin(p), np.cos(p)]])
    rot_y = np.array([[np.cos(y), 0, np.sin(y)], [0, 1, 0], [-np.sin(y), 0, np.cos(y)]])
    camera = centred @ (rot_y @ rot_x).T + np.array([offset[0], offset[1], distance])
    u = FOCAL * camera[:, 0] / camera[:, 2] + FRAME[0] / 2.0
    v = FOCAL * camera[:, 1] / camera[:, 2] + FRAME[1] / 2.0
    return np.column_stack([u, v])


# ------------------------------------------------------------------- geometry


def test_square_on_board_reads_as_untilted():
    grid = board_points()
    view = measure(project(grid, distance=0.5), grid, FRAME)
    assert view is not None
    assert view.tilt == pytest.approx(0.0, abs=1e-6)


@pytest.mark.parametrize("angle", [25.0, 35.0, -30.0])
def test_a_tilted_board_is_detected_as_tilted(angle):
    grid = board_points()
    view = measure(project(grid, distance=0.5, pitch_deg=angle), grid, FRAME)
    assert view is not None
    assert view.tilt >= TILT_MIN


def test_tilt_grows_with_angle():
    grid = board_points()
    tilts = [measure(project(grid, distance=0.5, pitch_deg=a), grid, FRAME).tilt for a in (5.0, 20.0, 40.0)]
    assert tilts[0] < tilts[1] < tilts[2]


def test_tilt_does_not_change_with_range():
    """The property one threshold depends on: the same physical tilt must read
    the same whether the board is held close or far."""
    grid = board_points()
    tilts = [measure(project(grid, distance=d, pitch_deg=30.0), grid, FRAME).tilt for d in (0.35, 0.6, 1.0)]
    assert max(tilts) - min(tilts) < 0.02
    assert all(t >= TILT_MIN for t in tilts)


def test_in_plane_rotation_is_not_tilt():
    """Rotating the board in its own plane changes no edge-length ratio."""
    grid = board_points()
    spun = grid.copy()
    theta = np.radians(30.0)
    rot = np.array([[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]])
    spun[:, :2] = grid[:, :2] @ rot.T
    view = measure(project(spun, distance=0.5), spun, FRAME)
    assert view is not None
    assert view.tilt == pytest.approx(0.0, abs=1e-6)


def test_a_partial_charuco_style_detection_measures_the_same_tilt():
    """Half the corners missing must not change the answer — that is the whole
    reason tilt comes from a fitted homography rather than the outer corners."""
    grid = board_points()
    image = project(grid, distance=0.5, pitch_deg=30.0)
    keep = np.arange(0, len(grid), 2)
    full = measure(image, grid, FRAME)
    partial = measure(image[keep], grid[keep], FRAME)
    assert full is not None and partial is not None
    assert partial.tilt == pytest.approx(full.tilt, abs=0.02)


def test_degenerate_input_returns_none():
    grid = board_points()
    assert measure(np.zeros((3, 2)), grid[:3], FRAME) is None
    assert measure(project(grid, 0.5), grid, (0, 0)) is None
    assert measure(np.zeros((len(grid), 2)), grid, FRAME) is None


# ------------------------------------------------------------------- distance


def test_closer_boards_have_a_larger_extent():
    grid = board_points()
    near = measure(project(grid, distance=0.3), grid, FRAME)
    far = measure(project(grid, distance=1.2), grid, FRAME)
    assert near.extent > far.extent


def test_distance_hints_bracket_the_usable_range():
    assert BoardView((0.5, 0.5), EXTENT_MIN - 0.05, 0.0).hint == TOO_FAR
    assert BoardView((0.5, 0.5), (EXTENT_MIN + EXTENT_MAX) / 2, 0.0).hint == IN_RANGE
    assert BoardView((0.5, 0.5), EXTENT_MAX + 0.05, 0.0).hint == TOO_CLOSE
    assert BoardView((0.5, 0.5), (EXTENT_MIN + EXTENT_MAX) / 2, 0.0).usable


def test_scale_bands_span_the_usable_range():
    bands = {BoardView((0.5, 0.5), e, 0.0).scale_band for e in np.linspace(EXTENT_MIN, EXTENT_MAX, 40)}
    assert bands == {0, 1, 2}


# -------------------------------------------------------------- board geometry

CHECKER = BoardGeometry.checkerboard((9, 6), 0.022)
CHARUCO = BoardGeometry.charuco(17, 9, 0.016)
FRAME_DIAG = float(np.hypot(*FRAME))


def test_charuco_cannot_be_held_as_far_as_a_checkerboard():
    """The finding this whole design turns on: ArUco markers must resolve enough
    pixels to decode, so the same sheet of paper reaches much less far."""
    _, checker_far = CHECKER.range_m(FOCAL, FRAME_DIAG)
    _, charuco_far = CHARUCO.range_m(FOCAL, FRAME_DIAG)
    assert charuco_far < checker_far
    assert CHARUCO.extent_min(FRAME_DIAG) > CHECKER.extent_min(FRAME_DIAG)


def test_a_board_needing_more_pixels_must_be_held_closer():
    fussy = BoardGeometry(squares=(8, 5), square_size_m=0.022, min_pixels_per_square=20.0)
    assert fussy.extent_min(FRAME_DIAG) == pytest.approx(2 * CHECKER.extent_min(FRAME_DIAG))


def test_the_usable_range_brackets_the_extent_limits():
    near, far = CHECKER.range_m(FOCAL, FRAME_DIAG)
    assert near < far
    # Held at the near limit the board spans EXTENT_MAX of the frame; at the far
    # limit it is down to the detection floor.
    for distance, expected in ((near, EXTENT_MAX), (far, CHECKER.extent_min(FRAME_DIAG))):
        view = measure(project(board_points(), distance=distance), board_points(), FRAME, CHECKER, FOCAL)
        assert view.extent == pytest.approx(expected, rel=1e-6)


def test_degenerate_optics_do_not_produce_a_range():
    assert CHECKER.range_m(0.0, FRAME_DIAG) == (0.0, 0.0)
    assert CHECKER.range_m(FOCAL, 0.0) == (0.0, 0.0)
    assert CHECKER.extent_min(0.0) == 0.0


# ------------------------------------------------------- distance round-trip


@pytest.mark.parametrize("distance", [0.15, 0.3, 0.55])
def test_the_reported_distance_recovers_the_true_one(distance):
    grid = board_points()
    view = measure(project(grid, distance=distance), grid, FRAME, CHECKER, FOCAL)
    assert view.approx_distance_m == pytest.approx(distance, rel=0.02)


def test_pixels_per_square_falls_off_with_range():
    grid = board_points()
    near_m, far_m = CHECKER.range_m(FOCAL, FRAME_DIAG)
    near = measure(project(grid, distance=near_m * 1.2), grid, FRAME, CHECKER, FOCAL)
    beyond = measure(project(grid, distance=far_m * 1.3), grid, FRAME, CHECKER, FOCAL)
    assert near.pixels_per_square > beyond.pixels_per_square
    assert near.hint == IN_RANGE
    assert beyond.hint == TOO_FAR
    assert beyond.pixels_per_square < CHECKER.min_pixels_per_square


def test_no_geometry_still_measures_but_reports_no_distance():
    """A caller that has not said which board it is must still get a usable
    view, not a crash and not a fabricated distance."""
    grid = board_points()
    view = measure(project(grid, distance=0.4), grid, FRAME)
    assert view is not None
    assert view.approx_distance_m == 0.0
    assert view.pixels_per_square == 0.0
    assert view.extent_min == DEFAULT_EXTENT_MIN


# ----------------------------------------------------------------------- zones


def test_board_position_maps_to_the_expected_zone():
    assert BoardView((0.1, 0.1), 0.5, 0.0).zone == (0, 0)
    assert BoardView((0.5, 0.5), 0.5, 0.0).zone == (1, 1)
    assert BoardView((0.95, 0.95), 0.5, 0.0).zone == (2, 2)


def test_a_board_at_the_frame_edge_stays_in_range():
    """Clamping matters: a centroid of exactly 1.0 must not index off the grid."""
    assert BoardView((1.0, 1.0), 0.5, 0.0).zone == (2, 2)
    assert BoardView((0.0, 0.0), 0.5, 0.0).zone == (0, 0)


def test_the_projected_centroid_lands_in_the_right_zone():
    grid = board_points()
    view = measure(project(grid, distance=0.5, offset=(-0.15, -0.1)), grid, FRAME)
    assert view.zone == (0, 0)


# -------------------------------------------------------------------- progress


def fill(tracker: CoverageTracker, count: int, zone=(0.5, 0.5), extent=0.55, tilt=0.0) -> None:
    for _ in range(count):
        tracker.add(BoardView(zone, extent, tilt))


def test_repeating_one_pose_never_completes():
    tracker = CoverageTracker(target_captures=10)
    fill(tracker, 40)
    progress = tracker.progress()
    assert not progress.complete
    assert progress.percent < 100.0
    assert "top-left" in progress.missing
    assert "tilted views" in progress.missing


def test_hitting_the_capture_target_alone_is_not_completion():
    tracker = CoverageTracker(target_captures=5)
    fill(tracker, 5)
    assert tracker.captures == 5
    assert not any(m.endswith("more captures") for m in tracker.progress().missing)
    assert not tracker.progress().complete


def test_progress_rises_as_zones_are_covered():
    tracker = CoverageTracker(target_captures=20)
    seen = [tracker.progress().percent]
    for x in (0.15, 0.5, 0.85):
        for y in (0.15, 0.5, 0.85):
            tracker.add(BoardView((x, y), 0.55, 0.0))
            seen.append(tracker.progress().percent)
    assert seen == sorted(seen)
    assert seen[-1] > seen[0]


def test_a_full_sweep_reaches_completion():
    tracker = CoverageTracker(target_captures=18)
    extents = [EXTENT_MAX - 0.05, (EXTENT_MIN + EXTENT_MAX) / 2, EXTENT_MIN + 0.05]
    for x in (0.15, 0.5, 0.85):
        for y in (0.15, 0.5, 0.85):
            for i, extent in enumerate(extents):
                tracker.add(BoardView((x, y), extent, TILT_MIN if i else 0.0))
    progress = tracker.progress()
    assert progress.complete, progress.missing
    assert progress.percent == pytest.approx(100.0)
    assert tracker.advice() == ""


def test_advice_names_what_is_short():
    tracker = CoverageTracker(target_captures=10)
    fill(tracker, 3)
    advice = tracker.advice()
    assert advice.startswith("Still needed:")
    assert len(advice.split(", ")) <= 3
