# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Short-lived visual correspondence, never an object detector or world map."""

import cv2
import numpy as np


class LocalTarget:
    """Keep real image features attached to a detected target.

    Box features come only from its perimeter: seeing the floor through a
    transparent interior does not establish that the container stayed put.
    Failed matches never update the reference, allowing a bounded retry.
    """

    def __init__(self, gray, anchor, box, *, edges_only=False):
        self.gray = gray
        self.anchor = tuple(anchor)
        self.edges_only = edges_only
        self.box = tuple(box)
        self.points = self._features(gray)

    def _features(self, gray):
        mask = np.zeros_like(gray)
        x0, y0, x1, y1 = map(int, self.box)
        h, w = gray.shape
        x0, x1 = max(0, x0), min(w, x1)
        y0, y1 = max(0, y0), min(h, y1)
        if x1 - x0 < 12 or y1 - y0 < 12:
            return None
        mask[y0:y1, x0:x1] = 255
        if self.edges_only:
            band = max(3, min(10, int(min(x1 - x0, y1 - y0) * 0.12)))
            mask[y0 + band : y1 - band, x0 + band : x1 - band] = 0
            # Corners on actual edges, not texture visible through the box.
            mask = cv2.bitwise_and(mask, cv2.dilate(cv2.Canny(gray, 40, 100), np.ones((3, 3), np.uint8)))
        return cv2.goodFeaturesToTrack(gray, 60, 0.02, 4, mask=mask, blockSize=3)

    def track(self, gray, predicted=None):
        pts = self.points
        if pts is None or len(pts) < 8:
            return None
        initial = None
        flags = 0
        if predicted is not None:
            initial = pts + np.array(predicted, np.float32) - np.array(self.anchor, np.float32)
            flags = cv2.OPTFLOW_USE_INITIAL_FLOW
        nxt, status, _ = cv2.calcOpticalFlowPyrLK(
            self.gray, gray, pts, initial, winSize=(21, 21), maxLevel=3, flags=flags
        )
        if nxt is None or status is None:
            return None
        valid = (status.ravel() == 1) & np.isfinite(nxt.reshape(-1, 2)).all(axis=1)
        src, dst = pts[valid], nxt[valid]
        if len(src) < 8:
            return None
        back, status, _ = cv2.calcOpticalFlowPyrLK(gray, self.gray, dst, None, winSize=(21, 21), maxLevel=3)
        if back is None or status is None:
            return None
        valid = (status.ravel() == 1) & (np.linalg.norm(back - src, axis=2).ravel() <= 1.5)
        src, dst = src[valid], dst[valid]
        if len(src) < 8:
            return None
        transform, inliers = cv2.estimateAffinePartial2D(src, dst, method=cv2.RANSAC, ransacReprojThreshold=2)
        if transform is None or inliers is None:
            return None
        good = inliers.ravel().astype(bool)
        if good.sum() < 8 or good.mean() < 0.7:
            return None
        spread = np.ptp(src[good].reshape(-1, 2), axis=0)
        width, height = self.box[2] - self.box[0], self.box[3] - self.box[1]
        if np.any(spread < np.array([width, height]) * (0.35 if self.edges_only else 0.2)):
            return None
        scale = np.linalg.norm(transform[:, 0])
        if not 0.8 <= scale <= 1.25:
            return None
        anchor = transform @ np.array([*self.anchor, 1.0])
        h, w = gray.shape
        if not np.isfinite(anchor).all() or not (0 <= anchor[0] < w and 0 <= anchor[1] < h):
            return None
        if predicted is not None and np.linalg.norm(anchor - np.array(predicted)) > 25:
            return None
        # Update only after all checks pass; keep features on the same object.
        corners = np.array([[self.box[0], self.box[1], 1], [self.box[2], self.box[3], 1]]) @ transform.T
        self.box = (*corners[0], *corners[1])
        self.gray, self.anchor, self.points = gray, tuple(anchor), dst[good]
        return self.anchor
