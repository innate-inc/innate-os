# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Track a rigid, weakly textured patch with verified image registration."""

import cv2
import numpy as np
from innate_skills.grasp_tracker import GraspPointTracker


def _edge_image(image):
    # Align color-image edge energy, not background intensity or a color blob.
    # Match edges at a scale that tolerates sensor blur and subpixel resampling.
    # Anchor visibility is checked separately on the unsmoothed color image.
    image = cv2.GaussianBlur(image.astype(np.float32) / 255, (0, 0), 2.0)
    gx = cv2.Sobel(image, cv2.CV_32F, 1, 0)
    gy = cv2.Sobel(image, cv2.CV_32F, 0, 1)
    energy = gx * gx + gy * gy
    return np.sqrt(energy.sum(axis=2) if energy.ndim == 3 else energy)


class RigidGraspTracker(GraspPointTracker):
    """Refresh appearance only after checking immutable original shape and anchor.

    Inherits the same cancellable caller/motion-worker contract as feature
    tracking. This path assumes a rigid object; it cannot track material
    deformation from silhouette geometry.
    """

    def __init__(self, hsv, box, point):
        image = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        self.guess = tuple(point)
        self.axis = None
        self.misses = 0
        self.points = None
        self.ok = False
        self.reason = "insufficient rigid shape support"
        photo = image.astype(np.float32) / 255
        image = _edge_image(image)
        x, y, w, h = map(int, box)
        x0, y0 = max(0, x - 18), max(0, y - 18)
        x1, y1 = min(image.shape[1], x + w + 18), min(image.shape[0], y + h + 18)
        self.template = image[y0:y1, x0:x1].copy()
        if not self.template.size:
            return
        self.photo = photo[y0:y1, x0:x1].copy()
        self.mask = np.zeros_like(self.template, np.uint8)
        self.mask[
            max(0, y - y0 - 6) : min(y1 - y0, y + h - y0 + 6), max(0, x - x0 - 6) : min(x1 - x0, x + w - x0 + 6)
        ] = 255
        if not self.mask.any():
            return
        # Exclude background inside the model box; this seed support stays fixed.
        labels = np.where(self.mask > 0, cv2.GC_PR_FGD, cv2.GC_BGD).astype(np.uint8)
        cv2.circle(labels, (round(point[0] - x0), round(point[1] - y0)), 3, cv2.GC_FGD, -1)
        background, foreground = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
        try:
            cv2.grabCut(np.uint8(self.photo * 255), labels, None, background, foreground, 3, cv2.GC_INIT_WITH_MASK)
        except cv2.error:
            return
        support = np.uint8((labels == cv2.GC_FGD) | (labels == cv2.GC_PR_FGD))
        self.mask &= cv2.dilate(support, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))) * 255
        edge_pixels = np.argwhere((self.template > 0.2 * np.max(self.template[self.mask > 0])) & (self.mask > 0))
        cells = {}
        for y, x in edge_pixels:
            key = (x // 6, y // 6)
            if key not in cells or self.template[y, x] > self.template[cells[key][1], cells[key][0]]:
                cells[key] = (x, y)
        self.shape_points = np.asarray(list(cells.values()), np.float32).reshape(-1, 2)
        self.anchor = np.array([point[0] - x0, point[1] - y0, 1.0])
        self.transform = np.array([[1.0, 0.0, x0], [0.0, 1.0, y0], [0.0, 0.0, 1.0]])
        self.guess = tuple(point)
        self.scale = 1.0
        self.angle = 0.0
        self.size = (w, h)
        self.ok = bool(np.max(self.template[self.mask > 0]) > 0.05) and self._supported(
            self.shape_points, self.anchor[:2]
        )
        self.points = self.shape_points.reshape(-1, 1, 2)
        self.reason = "seed" if self.ok else "no edge support"

    def _candidates(self, search, left, top, scales, angles):
        th, tw = self.template.shape
        small_search = cv2.resize(search, None, fx=0.5, fy=0.5)
        candidates = []
        for scale in scales:
            for angle in angles:
                matrix = cv2.getRotationMatrix2D((tw / 2, th / 2), float(angle), float(scale))
                corners = cv2.transform(np.array([[[0.0, 0.0], [tw, 0.0], [tw, th], [0.0, th]]], np.float32), matrix)[0]
                minimum = np.floor(corners.min(axis=0))
                maximum = np.ceil(corners.max(axis=0))
                matrix[:, 2] -= minimum
                out_size = tuple((maximum - minimum).astype(int))
                if out_size[0] >= search.shape[1] or out_size[1] >= search.shape[0]:
                    continue
                template = cv2.warpAffine(self.template, matrix, out_size)
                mask = cv2.warpAffine(self.mask, matrix, out_size, flags=cv2.INTER_NEAREST)
                scores = cv2.matchTemplate(
                    small_search,
                    cv2.resize(template, None, fx=0.5, fy=0.5),
                    cv2.TM_CCORR_NORMED,
                    mask=cv2.resize(mask, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_NEAREST),
                )
                scores = np.nan_to_num(scores, nan=-1.0, posinf=-1.0, neginf=-1.0)
                for _ in range(2):
                    _, score, _, loc = cv2.minMaxLoc(scores)
                    mat = matrix.copy()
                    mat[:, 2] += 2 * np.array(loc) + [left, top]
                    point = mat @ self.anchor
                    # A motion limit must not hide a competing visual match.
                    candidates.append((score, point, mat, scale, angle))
                    x, y = loc
                    r = max(5, int(min(self.size) * scale * 0.3))
                    scores[max(0, y - r) : y + r + 1, max(0, x - r) : x + r + 1] = -1
        return candidates

    def update(self, hsv):
        self.misses += 1
        if not self.ok:
            return None
        image = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        photo = image.astype(np.float32) / 255
        image = _edge_image(image)
        edges = image
        th, tw = self.template.shape
        # One-frame search bound. A stream gap requires external reacquisition.
        radius = 80
        left = max(0, int(self.guess[0] - radius - max(tw, th) * self.scale))
        top = max(0, int(self.guess[1] - radius - max(tw, th) * self.scale))
        right = min(image.shape[1], int(self.guess[0] + radius + max(tw, th) * self.scale))
        bottom = min(image.shape[0], int(self.guess[1] + radius + max(tw, th) * self.scale))
        search = edges[top:bottom, left:right]
        candidates = self._candidates(
            search,
            left,
            top,
            self.scale * np.array([0.85, 0.925, 1.0, 1.075, 1.15]),
            self.angle + np.array([-12.0, -6.0, 0.0, 6.0, 12.0]),
        )
        if candidates:
            # Refine the coarse pose before ECC; a valid small rotation can
            # otherwise fall between samples and converge to an edge mismatch.
            best = max(candidates, key=lambda candidate: candidate[0])
            candidates.extend(
                self._candidates(
                    search,
                    left,
                    top,
                    self.scale * np.linspace(0.85, 1.15, 9),
                    best[4] + np.array([-3.0, 0.0, 3.0]),
                )
            )
        if not candidates:
            self.reason = "no candidate"
            return None
        candidates.sort(key=lambda c: c[0], reverse=True)
        score, point, matrix, _, _ = candidates[0]
        alternatives = [
            c[0] for c in candidates[1:] if np.linalg.norm(c[1] - point) > min(self.size) * self.scale * 0.6
        ]
        second = max(alternatives, default=-1)
        if score - second < 0.08:
            self.reason = "ambiguous appearance"
            return None
        warped = cv2.warpAffine(image, matrix, (tw, th), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
        residual = np.eye(2, 3, dtype=np.float32)
        try:
            _, residual = cv2.findTransformECC(
                self.template,
                warped,
                residual,
                cv2.MOTION_AFFINE,
                (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5),
                self.mask,
                3,
            )
        except cv2.error:
            self.reason = "registration failed"
            return None
        try:
            backward = cv2.invertAffineTransform(residual).astype(np.float32)
            _, backward = cv2.findTransformECC(
                warped,
                self.template,
                backward,
                cv2.MOTION_AFFINE,
                (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 60, 1e-5),
                self.mask,
                3,
            )
            cycle = np.vstack((backward, [0, 0, 1])) @ np.vstack((residual, [0, 0, 1]))
            witnesses = np.array(
                [
                    [self.anchor[0], self.anchor[1], 1.0],
                    [tw * 0.25, th * 0.25, 1.0],
                    [tw * 0.75, th * 0.25, 1.0],
                    [tw * 0.75, th * 0.75, 1.0],
                    [tw * 0.25, th * 0.75, 1.0],
                ]
            )
            cycle_error = float(np.max(np.linalg.norm((witnesses @ cycle.T - witnesses)[:, :2], axis=1)))
            if not np.isfinite(cycle_error) or cycle_error > 1.5:
                self.reason = "nonreciprocal registration"
                return None
        except cv2.error:
            self.reason = "reverse registration failed"
            return None
        total = np.vstack((matrix, [0, 0, 1])) @ np.vstack((residual, [0, 0, 1]))
        if not np.isfinite(total).all():
            self.reason = "invalid registration"
            return None
        linear = total[:2, :2]
        singular = np.linalg.svd(linear, compute_uv=False)
        result = total[:2] @ self.anchor
        aligned = cv2.warpAffine(image, total[:2], (tw, th), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
        valid = self.mask > 0
        ref = self.template[valid]
        cur = aligned[valid]
        correlation = float(np.corrcoef(ref, cur)[0, 1]) if np.std(cur) > 1e-6 else -1
        ea = self.template[valid]
        eb = aligned[valid]
        edge_score = float(ea @ eb / (np.linalg.norm(ea) * np.linalg.norm(eb) + 1e-12))
        aligned_photo = cv2.warpAffine(photo, total[:2], (tw, th), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP)
        yy, xx = np.mgrid[:th, :tw]
        distance = np.hypot(xx - self.anchor[0], yy - self.anchor[1])
        center = distance <= 8
        ring = (distance >= 12) & (distance <= 24) & valid
        visibility_error = (
            float(
                np.percentile(
                    np.abs(
                        (self.photo[center] - np.median(self.photo[ring], axis=0))
                        - (aligned_photo[center] - np.median(aligned_photo[ring], axis=0))
                    ),
                    90,
                )
            )
            if ring.sum() > 20
            else 1.0
        )
        if (
            not np.isfinite(total).all()
            or np.linalg.det(linear) <= 0
            or singular[1] < 0.4
            or singular[0] > 4
            or singular[0] / singular[1] > 1.3
            or not (0.7 < np.sqrt(np.linalg.det(linear)) / self.scale < 1.4)
            or np.linalg.norm(result - np.asarray(self.guess)) > radius
            or not (5 <= result[0] < image.shape[1] - 5 and 5 <= result[1] < image.shape[0] - 5)
            or not np.isfinite([correlation, edge_score, visibility_error]).all()
            or correlation < 0.9
            or edge_score < 0.8
            or visibility_error > 0.08
        ):
            self.reason = "unverified registration"
            return None
        # Revalidate against immutable ORIGINAL edge geometry before updating
        # appearance. The material anchor never follows a template centroid.
        region = cv2.warpAffine(self.mask, total[:2], (image.shape[1], image.shape[0]), flags=cv2.INTER_NEAREST) > 0
        if not region.any():
            self.reason = "shape outside image"
            return None
        observed = np.argwhere((image > 0.2 * np.max(image[region])) & region)[:, ::-1].astype(np.float32)
        if len(observed) < 8 or len(self.shape_points) < 8:
            self.reason = "insufficient original shape support"
            return None
        projected = cv2.transform(self.shape_points[None], total[:2])[0]
        distances = np.sum((projected[:, None, :] - observed[None, :, :]) ** 2, axis=2)
        nearest = distances.argmin(axis=1)
        close = distances[np.arange(len(nearest)), nearest] < 2.5**2
        src = self.shape_points[close]
        dst = observed[nearest[close]]
        if len(src) < 8:
            self.reason = "original shape lost"
            return None
        fitted, inliers = cv2.estimateAffine2D(
            src, dst, method=cv2.RANSAC, ransacReprojThreshold=2.5, maxIters=1000, confidence=0.99, refineIters=10
        )
        if fitted is None or inliers is None or not np.isfinite(fitted).all():
            self.reason = "original shape inconsistent"
            return None
        keep = inliers.ravel().astype(bool)
        shape_ratio = float(keep.sum() / len(self.shape_points))
        src_good = src[keep]
        supported = self._supported(src_good, self.anchor[:2])
        anchor_difference = float(np.linalg.norm(fitted @ self.anchor - result))
        if shape_ratio < 0.65 or not supported or anchor_difference > 2.5:
            self.reason = "original shape unverified"
            return None
        self.template = aligned.copy()
        self.transform = total
        self.guess = tuple(result)
        self.scale = float(np.sqrt(np.linalg.det(linear)))
        self.angle = float(np.degrees(np.arctan2(linear[0, 1], linear[0, 0])))
        self.points = cv2.transform(self.shape_points.reshape(-1, 1, 2), total[:2])
        self.misses = 0
        self.reason = "registered"
        return self.guess
