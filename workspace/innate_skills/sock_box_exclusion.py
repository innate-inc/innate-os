# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Project the rehearsed box into pickup images; no model call or abort gate."""
import math

import cv2
import numpy as np
from innate_skills.box_marker import MarkerPose, camera_in_base, camera_matrix, load_config

from innate import vision
from innate.exceptions import SkillFailed


def box_vertices(pose, width=0.45, depth=0.15, height=0.12, margin=0.03):
    inward = -np.asarray(pose)[:2, 2]
    norm = np.linalg.norm(inward)
    if norm < 1e-6:
        return None
    inward = inward / norm
    right = np.array([inward[1], -inward[0]])
    origin = np.asarray(pose)[:2, 3]
    return np.array([
        [*(origin + right * r + inward * d), z]
        for r in (-width / 2 - margin, width / 2 + margin)
        for d in (-margin, depth + margin)
        for z in (0, height + margin)
    ])


def transform_vertices(vertices, odom, inverse=False):
    x, y, angle = odom
    c, s = math.cos(angle), math.sin(angle)
    rotation = np.array([[c, -s], [s, c]])
    out = vertices.copy()
    out[:, :2] = (vertices[:, :2] - [x, y]) @ rotation if inverse else vertices[:, :2] @ rotation.T + [x, y]
    return out


def projected_hull(vertices, tilt):
    camera = np.linalg.inv(camera_in_base(tilt))
    points = vertices @ camera[:3, :3].T + camera[:3, 3]
    # Clip cuboid edges at the camera plane, including partially visible boxes.
    visible = [p for p in points if p[2] >= 0.01]
    for i in range(8):
        for bit in (1, 2, 4):
            j = i ^ bit
            if j <= i:
                continue
            a, b = points[i], points[j]
            if (a[2] < 0.01) != (b[2] < 0.01):
                visible.append(a + (b - a) * ((0.01 - a[2]) / (b[2] - a[2])))
    if len(visible) < 3:
        return None
    pixels = np.asarray(visible) @ camera_matrix().T
    return cv2.convexHull((pixels[:, :2] / pixels[:, 2:]).astype(np.float32))


class SockBoxExclusion:
    def __init__(self, host):
        self.host = host
        self.world_vertices = None
        self.detector = None
        try:
            config = load_config()
            self.detector = MarkerPose(config['marker_id'], config['marker_size_m'], host._p['tilt_deg'])
        except (SkillFailed, ValueError, OSError, KeyError) as error:
            host.logger.info(f"[SockBox] box exclusion unavailable: {error}")

    def hull(self, image):
        odom = self.host.mobility.odom_xyt(self.host.odom)
        if odom is not None and not all(math.isfinite(v) for v in odom):
            odom = None
        pose = self.detector.detect(vision.b64_to_gray(image)) if self.detector is not None and image else None
        vertices = box_vertices(pose) if pose is not None else None
        if vertices is not None:
            self.world_vertices = transform_vertices(vertices, odom) if odom is not None else None
        elif self.world_vertices is not None and odom is not None:
            vertices = transform_vertices(self.world_vertices, odom, inverse=True)
        return projected_hull(vertices, self.host._p['tilt_deg']) if vertices is not None else None

    @staticmethod
    def contains(hull, point):
        return hull is not None and cv2.pointPolygonTest(hull, (float(point[0]), float(point[1])), False) >= 0

    def filter(self, candidates, image):
        hull = self.hull(image)
        kept = [c for c in candidates if not self.contains(hull, c[:2])]
        if len(kept) != len(candidates):
            self.host.logger.info(f"[SockBox] excluded {len(candidates) - len(kept)} box targets; {len(kept)} floor candidates remain")
        return kept
