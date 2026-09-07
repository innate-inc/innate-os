# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Track a selected material point, never a changing feature/blob centroid."""

import threading
import time
from contextlib import contextmanager

import cv2
import numpy as np


def make_grasp_tracker(hsv, box, point, *, rigid=False):
    tracker = GraspPointTracker(hsv, box, point)
    if rigid:
        # GFTT ranks corners relative to the strongest response, so a smooth
        # patch can yield many weak noise features. Require absolute contrast
        # and the same spatial support before choosing material optical flow.
        strength = cv2.cornerMinEigenVal(tracker.gray, blockSize=5)
        points = tracker.points.reshape(-1, 2) if tracker.points is not None else np.empty((0, 2))
        pixels = points.astype(int)
        strong = points[strength[pixels[:, 1], pixels[:, 0]] >= 1e-4]
        if not tracker._supported(strong, tracker.anchor):
            from innate_skills.rigid_grasp_tracker import RigidGraspTracker

            return RigidGraspTracker(hsv, box, point)
    return tracker


class GraspPointTracker:
    def __init__(self, hsv, box, point):
        self.gray = self._gray(hsv)
        self.anchor = np.asarray(point, dtype=np.float32)
        self.guess = tuple(point)
        self.axis = None
        self.misses = 0
        self.reason = "insufficient local texture"
        self.scale = 1.0
        x, y, w, h = box
        mask = np.zeros_like(self.gray)
        left, top = max(0, int(x) + 4), max(0, int(y) + 4)
        right, bottom = min(mask.shape[1], int(x + w) - 4), min(mask.shape[0], int(y + h) - 4)
        if right > left and bottom > top:
            mask[top:bottom, left:right] = 255
        local = np.zeros_like(mask)
        cv2.circle(local, tuple(np.round(self.anchor).astype(int)), 70, 255, -1)
        self.region = cv2.bitwise_and(mask, local)
        self.points = cv2.goodFeaturesToTrack(
            self.gray,
            maxCorners=100,
            qualityLevel=0.02,
            minDistance=5,
            mask=self.region,
            blockSize=5,
        )
        self.ok = self.points is not None and self._supported(self.points.reshape(-1, 2), self.anchor)
        self.original = self.points.copy() if self.ok else None

    @staticmethod
    def _gray(hsv):
        return cv2.cvtColor(cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR), cv2.COLOR_BGR2GRAY)

    @staticmethod
    def _supported(points, anchor):
        if len(points) < 8:
            return False
        # Reject a line or a one-sided cluster: it cannot constrain the grasp
        # point reliably when some features disappear behind a finger.
        if np.linalg.eigvalsh(np.cov(points.T))[0] < 16:
            return False
        hull = cv2.convexHull(points.astype(np.float32))
        return cv2.pointPolygonTest(hull, tuple(map(float, anchor)), True) >= -10

    def update(self, hsv):
        self.misses += 1
        if not self.ok:
            return None
        gray = self._gray(hsv)
        opts = dict(winSize=(31, 31), maxLevel=3, criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
        new, status, error = cv2.calcOpticalFlowPyrLK(self.gray, gray, self.points, None, **opts)
        if new is None:
            self.reason = "optical flow unavailable"
            return None
        back, back_status, _ = cv2.calcOpticalFlowPyrLK(gray, self.gray, new, None, **opts)
        if back is None:
            return None
        valid = (
            (status.ravel() == 1)
            & (back_status.ravel() == 1)
            & (error.ravel() < 25)
            & (np.linalg.norm(back - self.points, axis=2).ravel() < 1.5)
        )
        src = self.original.reshape(-1, 2)[valid]
        dst = new.reshape(-1, 2)[valid]
        self.reason = "insufficient consistent features"
        if len(src) < 8:
            return None
        transform, inliers = cv2.estimateAffinePartial2D(
            src,
            dst,
            method=cv2.RANSAC,
            ransacReprojThreshold=2.5,
            maxIters=1000,
            confidence=0.99,
            refineIters=10,
        )
        if transform is None or inliers is None or not np.isfinite(transform).all():
            return None
        keep = inliers.ravel().astype(bool)
        point = transform[:, :2] @ self.anchor + transform[:, 2]
        scale = float(np.linalg.norm(transform[:, 0]))
        height, width = gray.shape
        if (
            keep.mean() < 0.65
            or not self._supported(src[keep], self.anchor)
            or not self._supported(dst[keep], point)
            or not (0.4 <= scale <= 4 and 0.65 <= scale / self.scale <= 1.5)
            or np.linalg.norm(point - self.guess) > 100
            or not (5 <= point[0] < width - 5 and 5 <= point[1] < height - 5)
        ):
            self.reason = "unreliable grasp-point transform"
            return None
        # Fit from the original coordinates, so feature dropout cannot move
        # the anchor. Failed frames never contaminate the last accepted state.
        self.original = src[keep].reshape(-1, 1, 2).copy()
        self.points = dst[keep].reshape(-1, 1, 2).copy()
        self._refresh(gray, transform, point)
        self.gray = gray
        self.scale = scale
        self.guess = tuple(map(float, point))
        self.misses = 0
        self.reason = "tracked"
        return self.guess

    def _refresh(self, gray, transform, point):
        """Add texture inside the verified material patch, preserving its anchor.

        New corners acquire coordinates in the original reference frame through
        the inverse transform. Their centroid never becomes the grasp point.
        Refresh happens only after a geometrically accepted observation.
        """
        if len(self.points) >= 80:
            return
        height, width = gray.shape
        mask = cv2.warpAffine(self.region, transform, (width, height), flags=cv2.INTER_NEAREST)
        local = np.zeros_like(mask)
        cv2.circle(local, tuple(np.round(point).astype(int)), 70, 255, -1)
        mask = cv2.bitwise_and(mask, local)
        for p in self.points.reshape(-1, 2):
            cv2.circle(mask, tuple(np.round(p).astype(int)), 6, 0, -1)
        new = cv2.goodFeaturesToTrack(gray, 100 - len(self.points), 0.02, 6, mask=mask, blockSize=5)
        if new is None:
            return
        reference = cv2.transform(new, cv2.invertAffineTransform(transform))
        self.original = np.concatenate((self.original, reference))
        self.points = np.concatenate((self.points, new))

    @contextmanager
    def during_motion(self, read, decode, raw, gap_timeout=0.75):
        """Consume intermediate images while the caller performs a verified move.

        This worker only reads images and updates this tracker. The caller must
        not update the tracker until this context has joined the worker.
        """
        stop = threading.Event()
        state = {"raw": raw, "error": None, "gap": False, "last_frame_at": time.monotonic()}

        def track():
            try:
                last_frame = state["last_frame_at"]
                while not stop.is_set():
                    if time.monotonic() - last_frame >= gap_timeout:
                        state["gap"] = True
                    image = read()
                    if image and image is not state["raw"]:
                        state["raw"] = image
                        frame = decode(image)
                        if frame is not None:
                            last_frame = state["last_frame_at"] = time.monotonic()
                            if not state["gap"]:
                                self.update(frame)
                    stop.wait(0.025)
            except Exception as error:
                state["error"] = error

        worker = threading.Thread(target=track, daemon=True)
        worker.start()
        try:
            yield state
        finally:
            stop.set()
            worker.join(timeout=2)
            if worker.is_alive() or state["error"] is not None:
                # Discard the tracker on worker failure; never move from stale
                # state or reuse it while an old worker might still write it.
                self.ok = False
