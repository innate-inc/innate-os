# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Conservative sensor gates for the opt-in compact rigid RGB-D pickup path."""

import time

import cv2
import numpy as np


def revalidate_material_point(reference, current, box, point, *, now_ns):
    if reference is None or current is None:
        return None
    if any(not 0 <= time.monotonic() - t <= 0.5 for t in current.received_monotonic):
        return None
    # The model may take seconds. Only the reference is allowed to be historical.
    if not 0 <= (now_ns - current.stamp_ns) * 1e-9 <= 0.5 or current.stamp_ns <= reference.stamp_ns:
        return None
    if reference.generation != current.generation:
        return None
    if any(
        getattr(reference, k) != getattr(current, k)
        for k in ("frame_id", "image_size", "calibration_size", "k", "d", "r", "p")
    ):
        return None
    old_pose, new_pose = reference.odom_from_optical, current.odom_from_optical
    if old_pose is None or new_pose is None:
        return None
    old_pose, new_pose = np.asarray(old_pose), np.asarray(new_pose)
    if old_pose.shape != (7,) or new_pose.shape != (7,) or not np.isfinite([old_pose, new_pose]).all():
        return None
    if np.linalg.norm(old_pose[:3] - new_pose[:3]) > 0.002:
        return None
    if any(abs(np.linalg.norm(p[3:]) - 1) > 1e-5 for p in (old_pose, new_pose)):
        return None
    angle = 2 * np.arccos(np.clip(abs(old_pose[3:] @ new_pose[3:]), 0, 1))
    if angle > np.deg2rad(0.5):
        return None
    if len(box) != 4 or len(point) != 2 or not np.isfinite([*box, *point]).all():
        return None
    y0, x0, y1, x1 = box
    y, x = point
    if not (0 <= x0 < x < x1 <= 1000 and 0 <= y0 < y < y1 <= 1000):
        return None
    w, h = current.image_size
    u, v = x * w / 1000, y * h / 1000
    old_surface = reference.surface_point(u, v)
    new_surface = current.surface_point(u, v)
    if old_surface is None or new_surface is None or np.linalg.norm(np.subtract(old_surface, new_surface)) > 0.01:
        return None
    left, right = int(np.floor(x0 * w / 1000)), int(np.ceil(x1 * w / 1000))
    top, bottom = int(np.floor(y0 * h / 1000)), int(np.ceil(y1 * h / 1000))
    margin = max(4, int(0.1 * min(right - left, bottom - top)))
    left, right = max(0, left - margin), min(w, right + margin)
    top, bottom = max(0, top - margin), min(h, bottom + margin)
    if right - left < 12 or bottom - top < 12:
        return None
    old = cv2.imdecode(np.frombuffer(reference.jpeg, np.uint8), cv2.IMREAD_COLOR)
    new = cv2.imdecode(np.frombuffer(current.jpeg, np.uint8), cv2.IMREAD_COLOR)
    if old is None or new is None or old.shape != new.shape:
        return None
    residual = new.astype(np.float32) - old.astype(np.float32)
    crop = residual[top:bottom, left:right]
    # Modest uniform exposure changes are allowed; no spatial registration.
    shift = np.median(crop.reshape(-1, 3), axis=0)
    if np.max(np.abs(shift)) > 20:
        return None
    residual = np.abs(residual - shift)
    crop = residual[top:bottom, left:right]
    cy, cx = int(round(v)), int(round(u))
    local = residual[max(0, cy - 3) : min(h, cy + 4), max(0, cx - 3) : min(w, cx + 4)]
    if np.quantile(crop, 0.95) > 20 or crop.mean() > 6 or np.quantile(local, 0.95) > 20:
        return None
    return current.surface_point_in_base(u, v)


def compact_upper_surface(observation, box, point):
    """Bound visible compact geometry, with 10mm margin inside the 81mm jaw.

    This is deliberately a narrow visible-surface gate, not a reconstruction of
    hidden material. The model must identify a compact solid target and the
    final wrist view must independently show it between the open pads.
    """
    w, h = observation.image_size
    y0, x0, y1, x1 = box
    center = observation.surface_point_in_base(point[1] * w / 1000, point[0] * h / 1000)
    if center is None or not (0.26 <= center[0] <= 0.34 and abs(center[1]) <= 0.06 and 0.008 <= center[2] <= 0.06):
        return None
    samples = []
    for y in np.linspace(y0, y1, 7):
        for x in np.linspace(x0, x1, 7):
            surface = observation.surface_point_in_base(x * w / 1000, y * h / 1000)
            if surface is not None:
                samples.append(surface)
    if len(samples) < 35:
        return None
    samples = np.asarray(samples)
    # A silhouette box contains floor. Separate only the measured ground band;
    # retain every above-floor point, including neighboring obstacles.
    if samples[:, 2].min() < -0.01 or samples[:, 2].max() > 0.065:
        return None
    samples = samples[samples[:, 2] >= 0.008]
    if len(samples) < 10:
        return None
    yaw = np.arctan2(center[1] + 0.05285, center[0] - 0.086)
    jaw_axis = np.array([-np.sin(yaw), np.cos(yaw)])
    if np.ptp(samples[:, :2] @ jaw_axis) + 0.01 > 0.071:
        return None
    if np.max(np.ptp(samples[:, :2], axis=0)) + 0.01 > 0.071:
        return None
    # A front-face patch is lower than the observed top; it is not a center for
    # a vertical pinch. Leave only the sensor's few-mm uncertainty allowance.
    if center[2] < samples[:, 2].max() - 0.008:
        return None
    return center


def same_wrist_patch(old_jpeg, new_jpeg, box):
    """Stationary-view final veto revalidation; no tracking or coordinate warp."""
    old = cv2.imdecode(np.frombuffer(old_jpeg, np.uint8), cv2.IMREAD_COLOR)
    new = cv2.imdecode(np.frombuffer(new_jpeg, np.uint8), cv2.IMREAD_COLOR)
    if old is None or new is None or old.shape != new.shape:
        return False
    h, w = old.shape[:2]
    y0, x0, y1, x1 = box
    # Include the two pads and nearby scene, not only uniform object material.
    margin = 0.25 * max(x1 - x0, y1 - y0)
    x0, x1 = max(0, int((x0 - margin) * w / 1000)), min(w, int((x1 + margin) * w / 1000) + 1)
    y0, y1 = max(0, int((y0 - margin) * h / 1000)), min(h, int((y1 + margin) * h / 1000) + 1)
    residual = new[y0:y1, x0:x1].astype(float) - old[y0:y1, x0:x1].astype(float)
    if residual.size < 12 * 12 * 3:
        return False
    shift = np.median(residual.reshape(-1, 3), axis=0)
    delta = np.abs(residual - shift)
    return bool(np.max(np.abs(shift)) <= 20 and delta.mean() <= 6 and np.quantile(delta, 0.95) <= 20)
