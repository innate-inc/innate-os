"""Rigid material anchors under motion, appearance changes, and invalid evidence."""

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "workspace"))
from innate_skills.grasp_tracker import GraspPointTracker, make_grasp_tracker  # noqa: E402
from innate_skills.rigid_grasp_tracker import RigidGraspTracker  # noqa: E402

Y, X = np.mgrid[:480, :640]
BACKGROUND = np.uint8(175 + 5 * np.sin(X / 27) + 4 * np.cos(Y / 31))
MATERIAL = np.uint8(np.clip(70 + 0.12 * (X - 250) + 0.08 * (Y - 200), 0, 255))
MASK = np.zeros((480, 640), np.uint8)
cv2.rectangle(MASK, (250, 195), (389, 284), 255, -1)
SEED = np.where(MASK, MATERIAL, BACKGROUND).astype(np.uint8)
BOX, ANCHOR = (248, 193, 144, 94), (308.0, 241.0)


def hsv(image):
    return cv2.cvtColor(cv2.cvtColor(image, cv2.COLOR_GRAY2BGR), cv2.COLOR_BGR2HSV)


def frame(step):
    transform = cv2.getRotationMatrix2D((320, 240), step * 0.8, 1 + step * 0.01)
    transform[:, 2] += [step * 0.25, -step * 0.15]
    mask = cv2.warpAffine(MASK, transform, (640, 480))
    material = cv2.warpAffine(MATERIAL, transform, (640, 480))
    image = np.where(mask > 128, material, np.roll(BACKGROUND, step, axis=1)).astype(np.uint8)
    if step % 3 == 0:
        image = np.uint8(np.clip(image.astype(float) * 1.15 + 8, 0, 255))
    if step % 5 == 0:
        image = cv2.GaussianBlur(image, (5, 5), 1)
    return hsv(image), transform @ np.array([*ANCHOR, 1])


def test_rigid_reference_refresh_preserves_original_anchor_through_motion():
    tracker = make_grasp_tracker(hsv(SEED), BOX, ANCHOR, rigid=True)
    assert isinstance(tracker, RigidGraspTracker)
    anchor, shape = tracker.anchor.copy(), tracker.shape_points.copy()
    for step in range(1, 41):
        image, expected = frame(step)
        point = tracker.update(image)
        assert point is not None, (step, tracker.reason)
        assert np.linalg.norm(np.asarray(point) - expected) < 2
    assert np.array_equal(tracker.anchor, anchor)
    assert np.array_equal(tracker.shape_points, shape)
    assert tracker.misses == 0


@pytest.mark.parametrize("failure", ["blank", "missing", "occluded", "anchor", "partial_anchor", "rival", "deformed"])
def test_rejected_evidence_cannot_update_anchor_or_reference(failure):
    image = SEED.copy()
    if failure == "blank":
        image[:] = 175
    elif failure == "missing":
        image = BACKGROUND.copy()
    elif failure == "occluded":
        cv2.rectangle(image, (280, 190), (365, 290), 20, -1)
    elif failure in {"anchor", "partial_anchor"}:
        radius = 8 if failure == "anchor" else 4
        x, y = map(int, ANCHOR)
        cv2.rectangle(image, (x - radius, y - radius), (x + radius, y + radius), 20, -1)
    elif failure == "rival":
        image = BACKGROUND.copy()
        for dx in (-80, 80):
            image = np.where(np.roll(MASK, dx, axis=1), np.roll(MATERIAL, dx, axis=1), image).astype(np.uint8)
    else:
        mask = np.zeros_like(MASK)
        cv2.fillPoly(mask, [np.array([[250, 195], [390, 230], [360, 285], [280, 260]])], 255)
        image = np.where(mask, MATERIAL, BACKGROUND).astype(np.uint8)
    tracker = RigidGraspTracker(hsv(SEED), BOX, ANCHOR)
    for _ in range(2):
        point, transform, template = tracker.guess, tracker.transform.copy(), tracker.template.copy()
        assert tracker.update(hsv(image)) is None, failure
        assert tracker.guess == point
        assert np.array_equal(tracker.transform, transform)
        assert np.array_equal(tracker.template, template)
        assert tracker.misses == 1
        recovered, expected = frame(4)
        point = tracker.update(recovered)
        assert point is not None
        assert np.linalg.norm(np.asarray(point) - expected) < 2
        assert tracker.misses == 0


def test_textured_and_soft_targets_keep_feature_tracking():
    rng = np.random.default_rng(7)
    textured = cv2.GaussianBlur(rng.integers(0, 256, (480, 640), dtype=np.uint8), (3, 3), 0)
    assert type(make_grasp_tracker(hsv(textured), BOX, ANCHOR, rigid=True)) is GraspPointTracker
    assert type(make_grasp_tracker(hsv(SEED), BOX, ANCHOR, rigid=False)) is GraspPointTracker
    blank = RigidGraspTracker(hsv(np.zeros((480, 640), np.uint8)), BOX, ANCHOR)
    assert not blank.ok
    assert blank.update(hsv(SEED)) is None


def test_nonfinite_reverse_registration_is_rejected(monkeypatch):
    tracker = RigidGraspTracker(hsv(SEED), BOX, ANCHOR)
    original = cv2.findTransformECC
    calls = 0

    def register(*args, **kwargs):
        nonlocal calls
        calls += 1
        return (1.0, np.full((2, 3), np.nan, np.float32)) if calls == 2 else original(*args, **kwargs)

    monkeypatch.setattr(cv2, "findTransformECC", register)
    before = tracker.template.copy()
    assert tracker.update(hsv(SEED)) is None
    assert tracker.guess == ANCHOR
    assert np.array_equal(tracker.template, before)


def test_low_contrast_registration_tolerates_blur_without_anchor_drift():
    background = np.uint8(205 + 4 * np.sin(X / 13) * np.cos(Y / 17))
    material = np.uint8(235 + 0.02 * X + 0.01 * Y)
    mask = np.zeros_like(MASK)
    cv2.rectangle(mask, (250, 195), (389, 334), 255, -1)
    anchor = np.array([310.0, 264.0, 1.0])
    tracker = RigidGraspTracker(hsv(np.where(mask, material, background)), (248, 193, 144, 144), anchor[:2])
    for step in range(1, 9):
        transform = cv2.getRotationMatrix2D((320, 264), step * 0.6, 1 + step * 0.012)
        transform[:, 2] += [step * 0.7, -step * 0.4]
        coverage = cv2.warpAffine(mask.astype(np.float32) / 255, transform, (640, 480))
        surface = cv2.warpAffine(material, transform, (640, 480))
        image = np.uint8(coverage * surface + (1 - coverage) * np.roll(background, step * 2, axis=1))
        image = cv2.GaussianBlur(image, (0, 0), 2.0 if step % 4 == 0 else 0.3)
        point = tracker.update(hsv(image))
        assert point is not None, (step, tracker.reason)
        assert np.linalg.norm(np.asarray(point) - transform @ anchor) < 1
