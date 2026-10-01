"""Real OpenCV correspondence checks; no hardware commands."""

import importlib.util
import unittest
from pathlib import Path

import cv2
import numpy as np

spec = importlib.util.spec_from_file_location(
    "local_target", Path(__file__).parents[1] / "innate_skills/local_target.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
LocalTarget = module.LocalTarget


class LocalTargetTests(unittest.TestCase):
    def setUp(self):
        rng = np.random.default_rng(42)
        self.img = np.zeros((240, 320), np.uint8)
        self.img[70:170, 90:230] = cv2.GaussianBlur(rng.integers(0, 256, (100, 140), dtype=np.uint8), (3, 3), 0)
        self.box = (90, 70, 230, 170)

    def test_small_motion_and_turn_prediction(self):
        for dx, dy in [(5, 3), (65, 5)]:
            t = LocalTarget(self.img, (160, 120), self.box)
            image = cv2.warpAffine(self.img, np.float32([[1, 0, dx], [0, 1, dy]]), (320, 240))
            got = t.track(image, (160 + dx, 120 + dy))
            self.assertIsNotNone(got)
            self.assertLess(np.linalg.norm(np.array(got) - [160 + dx, 120 + dy]), 0.5)

    def test_occlusion_does_not_corrupt_reference(self):
        t = LocalTarget(self.img, (160, 120), self.box)
        self.assertIsNone(t.track(np.zeros_like(self.img), (160, 120)))
        self.assertEqual(t.anchor, (160, 120))
        self.assertIsNotNone(t.track(self.img, (160, 120)))

    def test_inconsistent_prediction_is_rejected(self):
        t = LocalTarget(self.img, (160, 120), self.box)
        self.assertIsNone(t.track(self.img, (290, 200)))

    def test_texture_only_inside_transparent_box_is_not_trackable(self):
        img = np.zeros_like(self.img)
        img[90:150, 110:210] = self.img[90:150, 110:210]
        t = LocalTarget(img, (160, 170), self.box, edges_only=True)
        self.assertIsNone(t.track(img, (160, 170)))

    def test_visible_box_perimeter_tracks_contact_anchor(self):
        t = LocalTarget(self.img, (160, 170), self.box, edges_only=True)
        image = cv2.warpAffine(self.img, np.float32([[1, 0, 7], [0, 1, 2]]), (320, 240))
        got = t.track(image, (167, 172))
        self.assertIsNotNone(got)
        self.assertLess(np.linalg.norm(np.array(got) - [167, 172]), 0.5)

    def test_blank_or_tiny_region_falls_back(self):
        for img, box in [(np.zeros_like(self.img), self.box), (self.img, (100, 100, 103, 103))]:
            self.assertIsNone(LocalTarget(img, (101, 101), box).track(img))
