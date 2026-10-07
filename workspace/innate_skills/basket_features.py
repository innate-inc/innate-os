# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Read-only, local woven-basket recognition and approximate visible-face pose.

No ROS, motion, cloud call or frame-to-frame state. Each detection must establish
new visual evidence. Camera coordinates: +x right, +y down, +z forward. The face
frame has +x along the visible face, +y down and +z into the basket. This does not
identify a unique basket heading: opposite woven faces can be indistinguishable.
"""

from dataclasses import dataclass
import json
import math
from pathlib import Path
import time

import cv2
import numpy as np


@dataclass(frozen=True)
class Camera:
    image_size: tuple
    matrix: np.ndarray
    distortion: np.ndarray

    @classmethod
    def load(cls, path):
        c = json.loads(Path(path).read_text())
        if c.get("distortion_model") != "plumb_bob":
            raise ValueError("Only calibrated plumb_bob images are supported")
        size = tuple(c["image_size"])
        k = np.asarray(c["camera_matrix"], dtype=np.float64)
        d = np.asarray(c["distortion"], dtype=np.float64).ravel()
        if (
            len(size) != 2
            or min(size) <= 0
            or k.shape != (3, 3)
            or not np.isfinite(k).all()
            or not np.isfinite(d).all()
            or k[0, 0] <= 0
            or k[1, 1] <= 0
            or d.size not in (4, 5, 8, 12, 14)
            or not np.allclose(k[2], [0, 0, 1])
        ):
            raise ValueError("Invalid camera calibration")
        return cls(size, k, d)

    def scaled(self, size):
        """Full-FOV resize only; a crop needs a separately adjusted calibration."""
        k = self.matrix.copy()
        k[0] *= size[0] / self.image_size[0]
        k[1] *= size[1] / self.image_size[1]
        return k


def rootsift(descriptors):
    if descriptors is None:
        return None
    return np.sqrt(
        descriptors / np.maximum(descriptors.sum(axis=1, keepdims=True), 1e-12)
    ).astype(np.float32)


def face_corners(width, height):
    if not math.isfinite(width) or not math.isfinite(height) or min(width, height) <= 0:
        raise ValueError("Face dimensions must be positive and finite")
    return np.array(
        [
            [-width / 2, -height / 2],
            [width / 2, -height / 2],
            [width / 2, height / 2],
            [-width / 2, height / 2],
        ],
        np.float32,
    )


def transform(points, h):
    return cv2.perspectiveTransform(
        np.asarray(points, np.float32).reshape(-1, 1, 2), h
    ).reshape(-1, 2)


def pose_candidates(object_xy, image_xy, k, distortion, width, height):
    """Return physically forward planar solutions; keep ambiguity explicit."""
    obj = np.column_stack([object_xy, np.zeros(len(object_xy))]).astype(np.float64)
    img = np.asarray(image_xy, np.float64)
    if len(obj) < 4:
        return []
    try:
        ok, rotations, translations, _ = cv2.solvePnPGeneric(
            obj, img, k, distortion, flags=cv2.SOLVEPNP_IPPE
        )
    except cv2.error:
        return []
    if not ok:
        return []
    results = []
    for rv, tv in zip(rotations, translations):
        try:
            rv, tv = cv2.solvePnPRefineLM(
                obj,
                img,
                k,
                distortion,
                rv.copy(),
                tv.copy(),
                criteria=(cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_COUNT, 50, 1e-8),
            )
        except cv2.error:
            continue
        if not np.isfinite(rv).all() or not np.isfinite(tv).all():
            continue
        r = cv2.Rodrigues(rv)[0]
        face = np.column_stack([face_corners(width, height), np.zeros(4)])
        camera_points = face @ r.T + tv.ravel()
        if not np.isfinite(camera_points).all() or np.any(camera_points[:, 2] <= 0):
            continue
        pred = cv2.projectPoints(obj, rv, tv, k, distortion)[0].reshape(-1, 2)
        error = float(np.sqrt(np.mean(np.sum((pred - img) ** 2, axis=1))))
        if not math.isfinite(error):
            continue
        normal = r[:, 2]
        # Face +z points away from the viewer, into the basket.
        if float(normal @ tv.ravel()) <= 0:
            continue
        rim = r @ np.array([0, -height / 2, 0]) + tv.ravel()
        results.append(
            {
                "reprojection_rms_px": error,
                "face_center_camera_m": tv.ravel().tolist(),
                "visible_rim_midpoint_camera_m": rim.tolist(),
                "rim_forward_depth_m": float(rim[2]),
                "rim_range_m": float(np.linalg.norm(rim)),
                "rim_right_offset_m": float(rim[0]),
                "face_normal_yaw_camera_deg": math.degrees(
                    math.atan2(normal[0], normal[2])
                ),
                "face_normal_camera": normal.tolist(),
                "rotation_vector": rv.ravel().tolist(),
            }
        )
    return sorted(results, key=lambda p: p["reprojection_rms_px"])


def add_basket_geometry(pose, face_width, height, depth):
    """Extrude the matched exterior face into the user-measured rectangular box.

    This is an approximate OUTER rim, not an inferred free drop opening.
    """
    r = cv2.Rodrigues(np.array(pose["rotation_vector"], dtype=float))[0]
    t = np.array(pose["face_center_camera_m"])
    rim_local = np.array(
        [
            [-face_width / 2, -height / 2, 0],
            [face_width / 2, -height / 2, 0],
            [face_width / 2, -height / 2, depth],
            [-face_width / 2, -height / 2, depth],
        ]
    )
    rim = rim_local @ r.T + t
    candidates = []
    for a, b in zip(rim, np.roll(rim, -1, axis=0)):
        edge = b - a
        fraction = np.clip(-a @ edge / (edge @ edge), 0, 1)
        candidates.append(a + fraction * edge)
    nearest = min(candidates, key=np.linalg.norm)
    pose.update(
        outer_rim_corners_camera_m=rim.tolist(),
        nearest_outer_rim_point_camera_m=nearest.tolist(),
        nearest_outer_rim_range_m=float(np.linalg.norm(nearest)),
        basket_center_camera_m=(t + r @ np.array([0, 0, depth / 2])).tolist(),
    )


class BasketDetector:
    def __init__(self, assets):
        self.assets = Path(assets)
        self.camera = Camera.load(self.assets / "calibration.json")
        config = json.loads((self.assets / "references.json").read_text())
        if config.get("version") != 1 or not config.get("references"):
            raise ValueError("Unsupported or empty reference set")
        self.features = cv2.SIFT_create(
            nfeatures=0, contrastThreshold=0.02, edgeThreshold=12
        )
        self.matcher = cv2.BFMatcher(cv2.NORM_L2)
        self.algorithm = "rootsift_full_resolution"
        dimensions = config["dimensions_m"]
        self.length, self.width = (
            float(dimensions["length"]),
            float(dimensions["width"]),
        )
        if not all(math.isfinite(v) and v > 0 for v in (self.length, self.width)):
            raise ValueError("Invalid basket dimensions")
        self.references = []
        for ref in config["references"]:
            if not (self.assets / ref["image"]).is_file():
                raise ValueError("Missing reference image: " + ref["image"])
            gray = cv2.imread(str(self.assets / ref["image"]), cv2.IMREAD_GRAYSCALE)
            if gray is None:
                raise ValueError("Missing reference image: " + ref["image"])
            q = np.asarray(ref["quad_px"], np.float32)
            width, height = float(ref["width_m"]), float(ref["height_m"])
            corners = face_corners(width, height)
            if not any(
                math.isclose(width, v) for v in (self.length, self.width)
            ) or not math.isclose(height, float(dimensions["height"])):
                raise ValueError(
                    "Reference face dimensions disagree with basket dimensions"
                )
            if (
                q.shape != (4, 2)
                or not np.isfinite(q).all()
                or not cv2.isContourConvex(q)
                or cv2.contourArea(q, oriented=True) < 100
            ):
                raise ValueError("Invalid reference face corners")
            mask = np.zeros_like(gray)
            cv2.fillConvexPoly(mask, q.astype(np.int32), 255)
            mask = cv2.erode(mask, np.ones((7, 7), np.uint8))
            kp, desc = self.features.detectAndCompute(gray, mask)
            if desc is None or len(kp) < 12:
                raise ValueError("Reference has too few features: " + ref["name"])
            origin = np.asarray(ref["crop_origin_px"], np.float32)
            ref_k = self.camera.scaled(ref["source_image_size"])
            corrected_q = cv2.undistortPoints(
                (q + origin).reshape(-1, 1, 2), ref_k, self.camera.distortion, P=ref_k
            ).reshape(-1, 2)
            to_metric = cv2.getPerspectiveTransform(
                corrected_q.astype(np.float32), corners
            )
            pts = np.array([p.pt for p in kp], np.float32) + origin
            pts = cv2.undistortPoints(
                pts.reshape(-1, 1, 2), ref_k, self.camera.distortion, P=ref_k
            ).reshape(-1, 2)
            self.references.append(
                dict(
                    ref,
                    descriptor=rootsift(desc),
                    metric_xy=transform(pts, to_metric),
                    corners=corners,
                )
            )

    def detect(self, image):
        started = time.perf_counter()
        result = {
            "detected": False,
            "reason": "no_supported_match",
            "algorithm": self.algorithm,
            "metric_accuracy_verified": False,
            "candidates": [],
        }
        if (
            image is None
            or image.dtype != np.uint8
            or image.ndim not in (2, 3)
            or min(image.shape[:2]) < 32
        ):
            raise ValueError("Expected a nonempty uint8 camera image")
        if image.ndim == 3 and image.shape[2] != 3:
            raise ValueError("Expected grayscale or BGR image")
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        h, w = gray.shape
        kp, desc = self.features.detectAndCompute(gray, None)
        desc = rootsift(desc)
        result["query_features"] = len(kp)
        feature_done = time.perf_counter()
        if desc is not None and len(kp) >= 12:
            query_xy = np.array([p.pt for p in kp], np.float32)
            k = self.camera.scaled((w, h))
            corrected = cv2.undistortPoints(
                query_xy.reshape(-1, 1, 2), k, self.camera.distortion, P=k
            ).reshape(-1, 2)
            self.matcher.clear()
            self.matcher.add([desc])
            self.matcher.train()
            for ref in self.references:
                pairs = self.matcher.knnMatch(ref["descriptor"], k=2)
                good = [
                    m
                    for pair in pairs
                    if len(pair) == 2
                    for m, n in [pair]
                    if m.distance < 0.8 * n.distance
                ]
                reverse = self.matcher.knnMatch(desc, ref["descriptor"], k=1)
                good = [
                    m
                    for m in good
                    if reverse[m.trainIdx]
                    and reverse[m.trainIdx][0].trainIdx == m.queryIdx
                ]
                # Unique query features prevent repeated wicker from multiplying votes.
                unique = {}
                for m in sorted(good, key=lambda m: m.distance):
                    unique.setdefault(m.trainIdx, m)
                good = list(unique.values())
                if len(good) < 12:
                    continue
                obj = ref["metric_xy"][[m.queryIdx for m in good]]
                raw = query_xy[[m.trainIdx for m in good]]
                dst = corrected[[m.trainIdx for m in good]]
                hom, inliers = cv2.findHomography(
                    obj, dst, cv2.RANSAC, 3.0, maxIters=2000, confidence=0.995
                )
                if hom is None or inliers is None or not np.isfinite(hom).all():
                    continue
                keep = inliers.ravel().astype(bool)
                count = int(keep.sum())
                if count < 12 or count / len(good) < 0.45:
                    continue
                coverage = cv2.contourArea(cv2.convexHull(obj[keep])) / (
                    ref["width_m"] * ref["height_m"]
                )
                if coverage < 0.12:
                    continue
                quad = transform(ref["corners"], hom)
                area = cv2.contourArea(quad, oriented=True)
                if (
                    not np.isfinite(quad).all()
                    or not cv2.isContourConvex(quad)
                    or area < 150
                    or area > 2 * w * h
                ):
                    continue
                solutions = pose_candidates(
                    obj[keep],
                    raw[keep],
                    k,
                    self.camera.distortion,
                    ref["width_m"],
                    ref["height_m"],
                )
                depth = (
                    self.width
                    if math.isclose(ref["width_m"], self.length)
                    else self.length
                )
                for solution in solutions:
                    add_basket_geometry(
                        solution, ref["width_m"], ref["height_m"], depth
                    )
                pose = solutions[0] if solutions else None
                # Recognition can succeed without a pose that fits a rigid rectangle.
                if pose is not None and pose["reprojection_rms_px"] > 4.0:
                    pose = None
                candidate = {
                    "reference": ref["name"],
                    "face_width_m": ref["width_m"],
                    "face_height_m": ref["height_m"],
                    "inliers": count,
                    "tentative_matches": len(good),
                    "reference_coverage": float(coverage),
                    "quad_undistorted_px": quad.tolist(),
                    "pose": pose,
                    "pose_alternatives": solutions[:2],
                    "pose_ambiguous": False,
                }
                if len(solutions) > 1:
                    n0, n1 = (np.array(p["face_normal_camera"]) for p in solutions[:2])
                    gap = math.degrees(math.acos(float(np.clip(n0 @ n1, -1, 1))))
                    candidate["pose_ambiguous"] = bool(
                        gap > 10
                        and solutions[1]["reprojection_rms_px"]
                        - solutions[0]["reprojection_rms_px"]
                        < 0.5
                    )
                result["candidates"].append(candidate)
        result["candidates"].sort(
            key=lambda c: c["inliers"] * math.sqrt(c["reference_coverage"]),
            reverse=True,
        )
        if result["candidates"]:
            best = result["candidates"][0]
            result.update(detected=True, reason="matched", best=best)
        result["feature_ms"] = (feature_done - started) * 1000
        result["processing_ms"] = (time.perf_counter() - started) * 1000
        return result


def draw_detection(image, result, camera):
    out = image.copy()
    if not result["detected"]:
        cv2.putText(
            out,
            "NO SUPPORTED BASKET MATCH",
            (20, 35),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 0, 255),
            2,
        )
        return out
    best = result["best"]
    k = camera.scaled((image.shape[1], image.shape[0]))
    quad = np.array(best["quad_undistorted_px"])
    rays = cv2.convertPointsToHomogeneous(quad).reshape(-1, 3) @ np.linalg.inv(k).T
    raw = cv2.projectPoints(rays, np.zeros(3), np.zeros(3), k, camera.distortion)[
        0
    ].reshape(-1, 2)
    cv2.polylines(out, [np.rint(raw).astype(np.int32)], True, (30, 220, 40), 3)
    lines = [
        f"BASKET FACE: {best['reference']} | {best['inliers']} inliers | {result['processing_ms']:.0f} ms"
    ]
    if best["pose"]:
        p = best["pose"]
        lines += [
            f"APPROX camera rim depth {p['rim_forward_depth_m']:.2f} m; right {p['rim_right_offset_m']:.2f} m",
            f"Face normal yaw {p['face_normal_yaw_camera_deg']:.1f} deg | physical accuracy UNVERIFIED",
        ]
        if best["pose_ambiguous"]:
            lines += ["PLANAR POSE AMBIGUOUS: multiple orientations fit"]
    else:
        lines += ["Pose unavailable: rectangular metric model did not fit"]
    for i, text in enumerate(lines):
        cv2.putText(
            out, text, (15, 30 + i * 28), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (0, 0, 0), 4
        )
        cv2.putText(
            out,
            text,
            (15, 30 + i * 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.58,
            (80, 255, 80),
            1,
        )
    return out
