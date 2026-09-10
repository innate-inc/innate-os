# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Tests for the checkerboard calibration arm.

These run against synthetically rendered boards with exactly known geometry, so
a detector or correspondence regression shows up as a metric blowing out rather
than as a silently worse calibration on the robot. Nothing here imports ROS.
"""

import cv2
import numpy as np
import pytest

from mars_cam.calibration_experiment import ExperimentRecorder, render_markdown, skew_summary
from mars_cam.calibration_validation import (
    ErrorStats,
    StereoCalibrationMatrices,
    ValidationPair,
    evaluate,
)
from mars_cam.checkerboard import CaptureDiagnostics, CheckerboardTarget, detect, measure, sharpness

PATTERN = (9, 6)
SQUARE = 0.022
IMAGE_SIZE = (640, 480)
BASELINE = 0.060
K = np.array([[520.0, 0.0, 320.0], [0.0, 520.0, 240.0], [0.0, 0.0, 1.0]])
D = np.zeros((5, 1))

# Right camera sits +BASELINE along the left camera's +x, so a point in left
# coordinates lands at X - [B,0,0] in right coordinates.
R_LEFT_TO_RIGHT = np.eye(3)
T_LEFT_TO_RIGHT = np.array([[-BASELINE], [0.0], [0.0]])


def board_texture(square_px: int = 48, quiet_zone: int = 60) -> tuple[np.ndarray, float]:
    """A 10x7-square board (9x6 inner corners) on a white quiet zone.

    Returns the texture and the metres-per-texture-pixel scale.
    """
    cols_sq, rows_sq = PATTERN[0] + 1, PATTERN[1] + 1
    board = np.zeros((rows_sq * square_px, cols_sq * square_px), np.uint8)
    for r in range(rows_sq):
        for c in range(cols_sq):
            if (r + c) % 2 == 0:
                board[r * square_px : (r + 1) * square_px, c * square_px : (c + 1) * square_px] = 255
    canvas = np.full((board.shape[0] + 2 * quiet_zone, board.shape[1] + 2 * quiet_zone), 255, np.uint8)
    canvas[quiet_zone : quiet_zone + board.shape[0], quiet_zone : quiet_zone + board.shape[1]] = board
    return canvas, SQUARE / square_px


def render(texture: np.ndarray, scale: float, quiet_zone: int, rvec: np.ndarray, tvec: np.ndarray, K_: np.ndarray):
    """Project the planar board texture into a camera view."""
    rotation, _ = cv2.Rodrigues(rvec)
    # Board origin (first inner corner) sits one square in from the texture edge.
    offset = np.array(
        [[1.0, 0.0, -(quiet_zone * scale + SQUARE)], [0.0, 1.0, -(quiet_zone * scale + SQUARE)], [0, 0, 1]]
    )
    texture_to_metres = np.array([[scale, 0.0, 0.0], [0.0, scale, 0.0], [0.0, 0.0, 1.0]])
    plane_to_camera = np.hstack([rotation[:, :2], tvec])
    homography = K_ @ plane_to_camera @ np.linalg.inv(offset) @ texture_to_metres
    return cv2.warpPerspective(
        texture, homography, IMAGE_SIZE, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=255
    )


def synthetic_views(rvec: np.ndarray, tvec: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """One stereo pair of the board at the given pose in the left camera frame."""
    texture, scale = board_texture()
    left = render(texture, scale, 60, rvec, tvec, K)
    rotation, _ = cv2.Rodrigues(rvec)
    right_rotation = R_LEFT_TO_RIGHT @ rotation
    right_tvec = R_LEFT_TO_RIGHT @ tvec + T_LEFT_TO_RIGHT
    right = render(texture, scale, 60, cv2.Rodrigues(right_rotation)[0], right_tvec, K)
    return left, right


def board_poses(count: int) -> list[tuple[np.ndarray, np.ndarray]]:
    rng = np.random.default_rng(7)
    poses = []
    for _ in range(count):
        rvec = rng.uniform(-0.25, 0.25, 3).reshape(3, 1)
        tvec = (np.array([-0.09, -0.07, 0.55]) + rng.uniform(-0.04, 0.04, 3)).reshape(3, 1)
        poses.append((rvec, tvec))
    return poses


# --------------------------------------------------------------------- target


def test_object_grid_matches_pattern_and_pitch():
    target = CheckerboardTarget(pattern=PATTERN, square_size=SQUARE)
    grid = target.object_grid()

    assert grid.shape == (54, 3)
    assert target.num_corners == 54
    assert np.allclose(grid[:, 2], 0.0)
    # Row-major: consecutive points in a row are one pitch apart in x.
    assert grid[1, 0] - grid[0, 0] == pytest.approx(SQUARE)
    assert grid[PATTERN[0], 1] - grid[0, 1] == pytest.approx(SQUARE)


def test_measured_pitch_override_scales_the_grid():
    grid = CheckerboardTarget(pattern=PATTERN, square_size=0.0218).object_grid()
    assert grid[1, 0] == pytest.approx(0.0218)


# ------------------------------------------------------------------ detection


def test_detects_the_complete_grid():
    left, _ = synthetic_views(np.zeros((3, 1)), np.array([[-0.09], [-0.07], [0.55]]))
    corners = detect(left, PATTERN)

    assert corners is not None
    assert corners.shape == (54, 1, 2)


def test_returns_none_when_the_board_is_absent():
    assert detect(np.full((480, 640), 255, np.uint8), PATTERN) is None


def test_canonical_order_always_starts_at_the_top_left_endpoint():
    """The anchor is what makes two eyes agree, so it must be deterministic."""
    left, _ = synthetic_views(np.zeros((3, 1)), np.array([[-0.09], [-0.07], [0.55]]))

    for image in (left, cv2.rotate(left, cv2.ROTATE_180)):
        corners = detect(image, PATTERN)
        assert corners is not None
        first, last = corners[0, 0], corners[-1, 0]
        assert first[0] + first[1] <= last[0] + last[1]


def test_rotating_the_board_reverses_the_order_rather_than_scrambling_it():
    """A 180-degree view is the same grid read from the opposite origin.

    The board pose absorbs that choice during calibration; what must never
    happen is an arbitrary reordering, which would break correspondence.
    """
    left, _ = synthetic_views(np.zeros((3, 1)), np.array([[-0.09], [-0.07], [0.55]]))
    upright = detect(left, PATTERN)
    flipped = detect(cv2.rotate(left, cv2.ROTATE_180), PATTERN)

    assert upright is not None and flipped is not None
    unrotated = np.stack([IMAGE_SIZE[0] - 1 - flipped[:, 0, 0], IMAGE_SIZE[1] - 1 - flipped[:, 0, 1]], axis=1)
    assert np.allclose(unrotated[::-1], upright.reshape(-1, 2), atol=1.0)


def test_left_and_right_corners_correspond():
    """Index i must name the same physical corner in both cameras."""
    left, right = synthetic_views(np.array([[0.15], [-0.2], [0.05]]), np.array([[-0.09], [-0.07], [0.55]]))
    corners_left = detect(left, PATTERN)
    corners_right = detect(right, PATTERN)

    assert corners_left is not None and corners_right is not None
    dy = np.abs(corners_left.reshape(-1, 2)[:, 1] - corners_right.reshape(-1, 2)[:, 1])
    # A pure-x baseline with identical intrinsics keeps matched corners on the
    # same row; a reversed ordering would scatter dy across the board height.
    assert dy.max() < 2.0


# ---------------------------------------------------------------- diagnostics


def test_measure_reports_geometry_for_a_detected_board():
    left, _ = synthetic_views(np.zeros((3, 1)), np.array([[-0.09], [-0.07], [0.55]]))
    view = measure(left, detect(left, PATTERN), stamp_sec=12.5)

    assert view.detected
    assert view.num_corners == 54
    assert view.stamp_sec == 12.5
    assert 0.0 < view.coverage_pct <= 100.0
    assert 0.0 < view.centroid_norm[0] < 1.0
    assert 0.0 < view.centroid_norm[1] < 1.0
    assert view.bbox[2] > view.bbox[0] and view.bbox[3] > view.bbox[1]


def test_measure_still_reports_quality_when_detection_fails():
    blank = np.full((480, 640), 128, np.uint8)
    view = measure(blank, None, stamp_sec=3.0)

    assert not view.detected
    assert view.num_corners == 0
    assert view.brightness == pytest.approx(128.0)
    assert view.coverage_pct == 0.0


def test_sharpness_separates_blurred_from_sharp():
    left, _ = synthetic_views(np.zeros((3, 1)), np.array([[-0.09], [-0.07], [0.55]]))
    assert sharpness(left) > sharpness(cv2.GaussianBlur(left, (11, 11), 5))


def test_skew_summary_reports_the_distribution():
    def diagnostics(skew: float) -> CaptureDiagnostics:
        view = measure(np.full((48, 64), 100, np.uint8), None, stamp_sec=1.0)
        return CaptureDiagnostics(
            index=0, attempt=0, accepted=True, reason="ok", split="train", stamp_skew_sec=skew, left=view, right=view
        )

    summary = skew_summary([diagnostics(s) for s in (0.001, -0.003, 0.002)])

    assert summary["count"] == 3
    assert summary["max_ms"] == pytest.approx(3.0)
    assert summary["median_ms"] == pytest.approx(2.0)


# ----------------------------------------------------------------- statistics


def test_error_stats_summarise_a_known_distribution():
    stats = ErrorStats.of(np.array([1.0, 2.0, 3.0, 4.0]))

    assert stats.count == 4
    assert stats.mean == pytest.approx(2.5)
    assert stats.rms == pytest.approx(np.sqrt(7.5))
    assert stats.median == pytest.approx(2.5)
    assert stats.maximum == pytest.approx(4.0)


def test_error_stats_tolerate_an_empty_sample():
    assert ErrorStats.of(np.empty(0)).count == 0


# ------------------------------------------------------- end-to-end pipeline


@pytest.fixture(scope="module")
def solved_rig():
    """Detect a synthetic capture set, calibrate on 80%, hold out 20%."""
    target = CheckerboardTarget(pattern=PATTERN, square_size=SQUARE)
    object_grid = target.object_grid().reshape(-1, 1, 3)

    detections = []
    for rvec, tvec in board_poses(15):
        left, right = synthetic_views(rvec, tvec)
        corners_left, corners_right = detect(left, PATTERN), detect(right, PATTERN)
        if corners_left is not None and corners_right is not None:
            detections.append((corners_left, corners_right))

    assert len(detections) >= 12, f"detector only found {len(detections)} of 15 synthetic boards"

    validation = {i for i in range(len(detections)) if (i + 1) % 5 == 0}
    train = [i for i in range(len(detections)) if i not in validation]

    objs = [object_grid] * len(train)
    _, K1, D1, _, _ = cv2.calibrateCamera(objs, [detections[i][0] for i in train], IMAGE_SIZE, None, None, flags=0)
    _, K2, D2, _, _ = cv2.calibrateCamera(objs, [detections[i][1] for i in train], IMAGE_SIZE, None, None, flags=0)
    _, K1, D1, K2, D2, R, T, _, _ = cv2.stereoCalibrate(
        objs,
        [detections[i][0] for i in train],
        [detections[i][1] for i in train],
        K1,
        D1,
        K2,
        D2,
        IMAGE_SIZE,
        flags=cv2.CALIB_FIX_INTRINSIC,
        criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 100, 1e-6),
    )
    return {
        "detections": detections,
        "validation": sorted(validation),
        "object_grid": object_grid,
        "matrices": (K1, D1, K2, D2, R, T),
    }


def _rectify_and_evaluate(solved_rig, negate_t: bool):
    K1, D1, K2, D2, R, T = solved_rig["matrices"]
    T_used = -T if negate_t else T
    R1, R2, P1, P2, _, _, _ = cv2.stereoRectify(
        K1, D1, K2, D2, IMAGE_SIZE, R, T_used, alpha=0, flags=cv2.CALIB_ZERO_DISPARITY
    )
    pairs = [
        ValidationPair(
            index=i,
            object_points=solved_rig["object_grid"],
            corners_left=solved_rig["detections"][i][0],
            corners_right=solved_rig["detections"][i][1],
            grid_shape=PATTERN,
        )
        for i in solved_rig["validation"]
    ]
    calib = StereoCalibrationMatrices(K1=K1, D1=D1, K2=K2, D2=D2, R1=R1, R2=R2, P1=P1, P2=P2)
    return evaluate(calib, pairs, IMAGE_SIZE, SQUARE)


def test_stereocalibrate_returns_negative_tx_for_a_right_mounted_second_camera(solved_rig):
    """Pins the convention the T-sign finding rests on."""
    _, _, _, _, _, T = solved_rig["matrices"]
    assert T[0, 0] < 0
    assert abs(abs(T[0, 0]) - BASELINE) < 0.002


def test_held_out_metrics_are_tight_on_a_synthetic_rig(solved_rig):
    report = _rectify_and_evaluate(solved_rig, negate_t=False)

    assert report.num_pairs == len(solved_rig["validation"])
    assert report.left_reprojection.rms < 0.5
    assert report.right_reprojection.rms < 0.5
    assert report.epipolar_dy.p95 < 0.5
    assert report.mean_spacing_mm == pytest.approx(SQUARE * 1000.0, abs=0.5)
    assert report.spacing_error_mm.mean < 0.5
    assert report.planarity_rms_mm.mean < 1.0


def test_validation_reports_positive_depth_with_the_sign_as_returned(solved_rig):
    report = _rectify_and_evaluate(solved_rig, negate_t=False)

    assert report.q_yields_positive_z
    assert report.median_depth_m > 0.0


def test_negating_t_flips_reconstructed_depth_but_not_distances(solved_rig):
    """The T negation is detectable as a depth-sign flip, and only that."""
    good = _rectify_and_evaluate(solved_rig, negate_t=False)
    negated = _rectify_and_evaluate(solved_rig, negate_t=True)

    assert not negated.q_yields_positive_z
    assert negated.median_depth_m == pytest.approx(-good.median_depth_m, rel=1e-6)
    # A global negation preserves every distance, which is why the bug hides.
    assert negated.spacing_error_mm.mean == pytest.approx(good.spacing_error_mm.mean, rel=1e-6)
    assert negated.epipolar_dy.rms == pytest.approx(good.epipolar_dy.rms, rel=1e-6)


def test_radial_bins_cover_every_validation_corner(solved_rig):
    report = _rectify_and_evaluate(solved_rig, negate_t=False)

    assert [b.label for b in report.radial_bins] == ["center", "middle", "outer"]
    assert sum(b.epipolar_dy.count for b in report.radial_bins) == report.epipolar_dy.count


def test_per_image_metrics_exist_for_each_validation_pair(solved_rig):
    report = _rectify_and_evaluate(solved_rig, negate_t=False)
    assert [m.index for m in report.per_image] == solved_rig["validation"]


# --------------------------------------------------------------- experiment io


def test_recorder_refuses_to_write_into_the_production_directory(tmp_path):
    with pytest.raises(ValueError, match="calibration_config"):
        ExperimentRecorder(tmp_path / "mars_calibration_config", "checkerboard")


def test_recorder_writes_candidate_not_production(tmp_path, solved_rig):
    K1, D1, K2, D2, R, T = solved_rig["matrices"]
    R1, R2, P1, P2, Q, _, _ = cv2.stereoRectify(
        K1, D1, K2, D2, IMAGE_SIZE, R, T, alpha=0, flags=cv2.CALIB_ZERO_DISPARITY
    )
    recorder = ExperimentRecorder(tmp_path / "calibration_experiments", "checkerboard")
    path = recorder.save_candidate_calibration(
        {
            "K1": K1,
            "D1": D1,
            "K2": K2,
            "D2": D2,
            "R": R,
            "T": T,
            "R1": R1,
            "R2": R2,
            "P1": P1,
            "P2": P2,
            "Q": Q,
            "image_width": IMAGE_SIZE[0],
            "image_height": IMAGE_SIZE[1],
        }
    )

    assert path.name == "stereo_calib_candidate.yaml"
    assert not (tmp_path / "calibration_experiments" / "stereo_calib.yaml").exists()
    stored = cv2.FileStorage(str(path), cv2.FileStorage_READ)
    assert int(stored.getNode("version").real()) == 2
    assert np.allclose(stored.getNode("K1").mat(), K1)
    stored.release()


def test_recorder_writes_capture_metadata(tmp_path):
    view = measure(np.full((48, 64), 100, np.uint8), None, stamp_sec=5.0)
    diagnostics = [
        CaptureDiagnostics(
            index=0,
            attempt=1,
            accepted=True,
            reason="ok",
            split="train",
            stamp_skew_sec=0.004,
            left=view,
            right=view,
        )
    ]
    recorder = ExperimentRecorder(tmp_path / "calibration_experiments", "checkerboard")
    json_path, csv_path = recorder.write_captures(diagnostics)

    assert "stamp_skew_sec" in json_path.read_text()
    header = csv_path.read_text().splitlines()[0]
    for column in ("left_sharpness", "right_brightness", "left_coverage_pct", "left_centroid_x_norm", "split"):
        assert column in header


def test_markdown_report_renders_all_sections(solved_rig):
    text = render_markdown(_rectify_and_evaluate(solved_rig, negate_t=False))

    assert "Held-out stereo calibration validation" in text
    assert "Error versus image location" in text
    assert "Per validation image" in text
    assert "|dy|" not in text  # unescaped pipes would break the tables
