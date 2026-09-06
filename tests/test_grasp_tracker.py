"""Geometric behavior on images, including feature dropout and failed frames."""

import importlib.util
from pathlib import Path

import cv2
import numpy as np

spec = importlib.util.spec_from_file_location(
    "grasp_tracker", Path(__file__).parents[1] / "workspace/innate_skills/grasp_tracker.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def hsv(gray):
    return cv2.cvtColor(cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR), cv2.COLOR_BGR2HSV)


def test_anchor_survives_motion_dropout_and_rejected_blank():
    rng = np.random.default_rng(5)
    gray = cv2.GaussianBlur(rng.integers(0, 256, (480, 640), dtype=np.uint8), (3, 3), 0)
    point = np.array([300.0, 240.0])
    tracker = module.GraspPointTracker(hsv(gray), (220, 160, 160, 160), point)
    assert tracker.ok
    transform = cv2.getRotationMatrix2D(tuple(point), 7, 1.12)
    transform[:, 2] += [35, 22]
    moved = cv2.warpAffine(gray, transform, (640, 480))
    moved[195:210, 280:295] = 0  # remove a subset of the original features
    expected = transform[:, :2] @ point + transform[:, 2]
    assert np.linalg.norm(np.array(tracker.update(hsv(moved))) - expected) < 1
    previous = tracker.guess
    assert tracker.update(hsv(np.zeros_like(gray))) is None
    assert tracker.guess == previous
    assert tracker.update(hsv(moved)) is not None
    assert np.linalg.norm(np.array(tracker.guess) - expected) < 1


def test_textureless_seed_is_rejected():
    tracker = module.GraspPointTracker(hsv(np.zeros((480, 640), np.uint8)), (200, 100, 200, 200), (300, 200))
    assert not tracker.ok
    assert tracker.update(hsv(np.zeros((480, 640), np.uint8))) is None


def test_motion_worker_tracks_intermediate_frames_and_joins():
    import threading
    import time

    rng = np.random.default_rng(7)
    gray = cv2.GaussianBlur(rng.integers(0, 256, (480, 640), dtype=np.uint8), (3, 3), 0)
    tracker = module.GraspPointTracker(hsv(gray), (220, 160, 160, 160), (300, 240))
    seen = threading.Event()
    current = [object()]
    images = {current[0]: hsv(gray)}

    def decode(raw):
        result = images[raw]
        seen.set()
        return result

    with tracker.during_motion(lambda: current[0], decode, None):
        for shift in range(0, 81, 8):
            seen.clear()
            raw = object()
            images[raw] = hsv(cv2.warpAffine(gray, np.float32([[1, 0, shift], [0, 1, 0]]), (640, 480)))
            current[0] = raw
            assert seen.wait(1)
            time.sleep(0.04)
    assert tracker.ok
    assert np.linalg.norm(np.array(tracker.guess) - [380, 240]) < 1
    point = tracker.guess
    time.sleep(0.05)
    assert tracker.guess == point


def test_refresh_keeps_material_anchor_through_long_descent():
    # Repeated zoom and partial occlusion discard original corners. New texture
    # must replenish support without replacing the chosen material point.
    rng = np.random.default_rng(19)
    gray = cv2.GaussianBlur(rng.integers(0, 256, (480, 640), dtype=np.uint8), (3, 3), 0)
    anchor = np.array([300.0, 240.0])
    tracker = module.GraspPointTracker(hsv(gray), (220, 160, 160, 160), anchor)
    initial_ids = tracker.original.copy()
    for i in range(1, 41):
        transform = cv2.getRotationMatrix2D(tuple(anchor), i * 0.15, 1 + i * 0.035)
        transform[:, 2] += [-i * 0.4, i * 0.15]
        frame = cv2.warpAffine(gray, transform, (640, 480))
        expected = transform[:, :2] @ anchor + transform[:, 2]
        result = tracker.update(hsv(frame))
        assert result is not None, (i, tracker.reason)
        assert np.linalg.norm(np.asarray(result) - expected) < 1.5
    assert len(tracker.points) >= 40
    assert any(np.min(np.linalg.norm(initial_ids - p, axis=2)) > 1 for p in tracker.original)
    previous = tracker.guess
    assert tracker.update(hsv(np.zeros_like(gray))) is None
    assert tracker.guess == previous


def test_motion_camera_gap_latches_and_does_not_bridge_missing_motion():
    import time

    rng = np.random.default_rng(3)
    gray = rng.integers(0, 256, (480, 640), dtype=np.uint8)
    tracker = module.GraspPointTracker(hsv(gray), (220, 160, 160, 160), (300, 240))
    current = [object()]
    original = tracker.guess
    with tracker.during_motion(lambda: current[0], lambda _: hsv(gray), current[0], gap_timeout=0.05) as state:
        deadline = time.monotonic() + 1
        while not state["gap"] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert state["gap"]
        current[0] = object()  # camera returns; the gap must stay latched
        time.sleep(0.08)
    assert state["gap"]
    assert tracker.guess == original
    assert tracker.misses == 0  # no speculative update across the interruption
