"""Geometry contracts and recognition failure cases; no robot services or motion."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workspace/innate_skills"))
from basket_features import (  # noqa: E402 - import from the accompanying checkout/package
    BasketDetector,
    reference_plane_mapping,
    transform,
    Camera,
    face_corners,
    pose_candidates,
    add_basket_geometry,
)

ASSETS = ROOT / "workspace/config/basket_features"


class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.k = np.array([[400.0, 0, 640], [0, 401.0, 360], [0, 0, 1]])
        self.d = np.array([0.006, -0.025, -0.001, -0.0013, 0.004])
        x, y = np.meshgrid(np.linspace(-0.25, 0.25, 9), np.linspace(-0.08, 0.08, 5))
        self.xy = np.column_stack([x.ravel(), y.ravel()]).astype(np.float64)

    def project(self, xy, yaw, t):
        obj = np.column_stack([xy, np.zeros(len(xy))])
        return cv2.projectPoints(
            obj,
            np.array([0.05, np.deg2rad(yaw), 0.02]),
            np.array(t, dtype=float),
            self.k,
            self.d,
        )[0].reshape(-1, 2)

    def test_metric_pose_at_close_and_far_ranges_and_angles(self):
        for z in (0.22, 0.5, 1.0):
            for yaw in (-40.0, 0.0, 35.0):
                with self.subTest(z=z, yaw=yaw):
                    t = [0.035, 0.10, z]
                    pix = self.project(self.xy, yaw, t)
                    visible = (
                        (pix[:, 0] >= 0)
                        & (pix[:, 0] < 1280)
                        & (pix[:, 1] >= 0)
                        & (pix[:, 1] < 720)
                    )
                    solutions = pose_candidates(
                        self.xy[visible], pix[visible], self.k, self.d, 0.5, 0.16
                    )
                    self.assertTrue(solutions)
                    np.testing.assert_allclose(
                        solutions[0]["face_center_camera_m"], t, atol=1e-5
                    )
                    self.assertLess(solutions[0]["reprojection_rms_px"], 1e-4)

    def test_calibrated_reference_plane_preserves_known_metric_points(self):
        corners = face_corners(0.5, 0.16)
        for yaw in (-35, 0, 35):
            pixels = self.project(self.xy, yaw, [0.04, 0.05, 0.7])
            quad = self.project(corners, yaw, [0.04, 0.05, 0.7])
            mapping, rms = reference_plane_mapping(quad, self.k, self.d, 0.5, 0.16)
            undistorted = cv2.undistortPoints(
                pixels.reshape(-1, 1, 2), self.k, self.d, P=self.k
            ).reshape(-1, 2)
            np.testing.assert_allclose(
                transform(undistorted, mapping), self.xy, atol=1e-6
            )
            self.assertLess(rms, 1e-4)

    def test_failed_live_frame_reference_warp_regression(self):
        data = json.loads(
            (ROOT / "tests/fixtures/basket_pose_reference_warp.json").read_text()
        )
        rk = np.array(data["reference_camera_matrix"])
        qk = np.array(data["query_camera_matrix"])
        dist = np.array(data["distortion"])
        quad = np.array(data["reference_quad_px"])
        points = np.array(data["reference_points_px"])
        query = np.array(data["query_points_px"])
        undistorted = cv2.undistortPoints(
            points.reshape(-1, 1, 2), rk, dist, P=rk
        ).reshape(-1, 2)
        mapping, reference_rms = reference_plane_mapping(quad, rk, dist, 0.5, 0.16)
        metric = transform(undistorted, mapping)
        result = pose_candidates(metric, query, qk, dist, 0.5, 0.16)
        self.assertGreater(reference_rms, 4)  # annotation inconsistency is visible
        self.assertLess(result[0]["reprojection_rms_px"], 2.5)
        self.assertGreater(result[0]["face_center_camera_m"][2], 0.24)
        self.assertLess(result[0]["face_center_camera_m"][2], 0.28)
        # Reproduce the exact old defect on the same unchanged matches.
        uq = cv2.undistortPoints(quad.reshape(-1, 1, 2), rk, dist, P=rk).reshape(-1, 2)
        free_warp = cv2.getPerspectiveTransform(
            uq.astype(np.float32), face_corners(0.5, 0.16)
        )
        rejected = pose_candidates(
            transform(undistorted, free_warp), query, qk, dist, 0.5, 0.16
        )
        self.assertGreater(rejected[0]["reprojection_rms_px"], 6)

    def test_partial_visible_face_still_has_metric_correspondences(self):
        xy = self.xy[self.xy[:, 1] < 0.02]
        pix = self.project(xy, 25.0, [0.02, 0.12, 0.25])
        solutions = pose_candidates(xy, pix, self.k, self.d, 0.5, 0.16)
        np.testing.assert_allclose(
            solutions[0]["face_center_camera_m"], [0.02, 0.12, 0.25], atol=1e-5
        )

    def test_nonuniform_full_frame_resize_preserves_pose(self):
        t = [0.03, 0.12, 0.45]
        pix = self.project(self.xy, 15.0, t)
        cam = Camera((1280, 720), self.k, self.d)
        small_k = cam.scaled((640, 480))
        small_pix = pix * np.array([0.5, 2 / 3])
        result = pose_candidates(self.xy, small_pix, small_k, self.d, 0.5, 0.16)
        np.testing.assert_allclose(result[0]["face_center_camera_m"], t, atol=1e-5)

    def test_full_outer_rim_geometry_and_nearest_point(self):
        pose = {"rotation_vector": [0, 0, 0], "face_center_camera_m": [0, 0.1, 0.25]}
        add_basket_geometry(pose, 0.5, 0.16, 0.3)
        np.testing.assert_allclose(
            pose["nearest_outer_rim_point_camera_m"], [0, 0.02, 0.25], atol=1e-8
        )
        np.testing.assert_allclose(pose["basket_center_camera_m"], [0, 0.1, 0.4])
        self.assertAlmostEqual(pose["nearest_outer_rim_range_m"], np.hypot(0.02, 0.25))

    def test_invalid_dimensions_and_calibration(self):
        for width in (0, -1, float("nan")):
            with self.assertRaises(ValueError):
                face_corners(width, 0.16)
        cfg = json.loads((ASSETS / "calibration.json").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cal.json"
            cfg["camera_matrix"][0][0] = 0
            path.write_text(json.dumps(cfg))
            with self.assertRaises(ValueError):
                Camera.load(path)


class RecognitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cv2.setNumThreads(2)
        cls.detector = BasketDetector(ASSETS)

    def test_blank_and_low_texture_no_detection(self):
        for level in (0, 128, 255):
            result = self.detector.detect(np.full((480, 640, 3), level, np.uint8))
            self.assertFalse(result["detected"])
            self.assertNotIn("best", result)

    def test_invalid_frames(self):
        for frame in (
            None,
            np.zeros((0, 640), np.uint8),
            np.zeros((480, 640), float),
            np.zeros((480, 640, 4), np.uint8),
        ):
            with self.assertRaises(ValueError):
                self.detector.detect(frame)

    def test_no_previous_detection_carried_into_blank_frame(self):
        # Construct a reference-only scene to exercise detection then target loss.
        ref = self.detector.references[0]
        crop = cv2.imread(str(ASSETS / ref["image"]))
        scene = np.zeros((480, 640, 3), np.uint8)
        h, w = crop.shape[:2]
        scene[200 : 200 + h, 180 : 180 + w] = crop
        self.assertTrue(self.detector.detect(scene)["detected"])
        self.assertFalse(self.detector.detect(np.zeros_like(scene))["detected"])

    def test_missing_reference_is_explicit_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "calibration.json").write_bytes(
                (ASSETS / "calibration.json").read_bytes()
            )
            (root / "references.json").write_bytes(
                (ASSETS / "references.json").read_bytes()
            )
            with self.assertRaisesRegex(ValueError, "Missing reference"):
                BasketDetector(root)


if __name__ == "__main__":
    unittest.main()
