# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Read-only head RGB-D measurements; no ROS, pose or motion assumptions.

Exact common capture stamps are required; independently rendered or acquired
streams are not associated by timestamp proximity. A measurement describes a
visible surface in the raw optical frame, not a grasp pose or free-space proof.
"""

from dataclasses import dataclass

import numpy as np


def stamp_ns(msg):
    return msg.header.stamp.sec * 1_000_000_000 + msg.header.stamp.nanosec


def decode_depth(msg):
    """Honor ROS row stride and byte order, preserving the encoding's units."""
    types = {"16UC1": "u2", "mono16": "u2", "32FC1": "f4", "mono32": "f4"}
    if msg.encoding not in types or msg.height <= 0 or msg.width <= 0:
        return None
    dtype = np.dtype((">" if msg.is_bigendian else "<") + types[msg.encoding])
    if msg.step < msg.width * dtype.itemsize or len(msg.data) != msg.step * msg.height:
        return None
    array = np.ndarray((msg.height, msg.width), dtype=dtype, buffer=msg.data, strides=(msg.step, dtype.itemsize))
    array.flags.writeable = False
    return array


@dataclass(frozen=True)
class RgbdObservation:
    jpeg: bytes
    depth_m: np.ndarray
    stamp_ns: int
    frame_id: str
    received_monotonic: tuple[float, float, float]
    image_size: tuple[int, int]
    calibration_size: tuple[int, int]
    k: tuple[float, ...]
    d: tuple[float, ...]
    r: tuple[float, ...]
    p: tuple[float, ...]
    generation: int = 0
    base_from_optical: tuple[float, ...] | None = None
    """Capture-time base_link transform: tx,ty,tz,qx,qy,qz,qw, or unavailable."""
    odom_from_optical: tuple[float, ...] | None = None
    """Capture-time camera pose in odom, for checking whether the view moved."""

    @classmethod
    def from_messages(cls, rgb, depth, info, received, *, now_ns, now_monotonic, max_age=0.5, generation=0):
        """Bind a same-stamp triplet, rejecting stale and unsupported metadata.

        The caller must also ensure no newer calibration has superseded info.
        ROS age bounds queued messages; monotonic age bounds locally cached ones.
        A driver stamp establishes pipeline association, not exposure timing.
        """
        import cv2

        stamps = (stamp_ns(rgb), stamp_ns(depth), stamp_ns(info))
        if not np.isfinite(max_age) or max_age <= 0 or stamps[0] <= 0 or len(set(stamps)) != 1:
            return None
        if not 0 <= (now_ns - stamps[0]) * 1e-9 <= max_age:
            return None
        if len(received) != 3 or any(not 0 <= now_monotonic - t <= max_age for t in received):
            return None
        if not rgb.header.frame_id or len({m.header.frame_id for m in (rgb, depth, info)}) != 1:
            return None
        if info.width <= 0 or info.height <= 0 or info.distortion_model not in ("plumb_bob", "rational_polynomial"):
            return None
        if (
            info.binning_x > 1
            or info.binning_y > 1
            or any((info.roi.x_offset, info.roi.y_offset, info.roi.width, info.roi.height, info.roi.do_rectify))
        ):
            return None
        if len(info.k) != 9 or len(info.r) != 9 or len(info.p) != 12 or len(info.d) not in (4, 5, 8):
            return None
        if not np.isfinite([*info.k, *info.d, *info.r, *info.p]).all():
            return None
        k, r, p = np.reshape(info.k, (3, 3)), np.reshape(info.r, (3, 3)), np.reshape(info.p, (3, 4))
        if min(k[0, 0], k[1, 1], p[0, 0], p[1, 1]) <= 0:
            return None
        if not (
            np.allclose([k[0, 1], k[1, 0], p[0, 1], p[1, 0]], 0)
            and np.allclose(k[2], [0, 0, 1])
            and np.allclose(p[2], [0, 0, 1, 0])
            and np.allclose(p[:, 3], 0)
            and np.allclose(r.T @ r, np.eye(3), atol=1e-5)
            and np.isclose(np.linalg.det(r), 1, atol=1e-5)
        ):
            return None
        decoded = decode_depth(depth)
        if decoded is None or depth.encoding not in ("16UC1", "32FC1"):
            return None
        jpeg = bytes(rgb.data)
        if not jpeg:
            return None
        image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            return None
        meters = decoded.astype(np.float32) * (0.001 if depth.encoding == "16UC1" else 1.0)
        # Immutable bytes back the array, so consumers cannot re-enable writes.
        meters = np.frombuffer(meters.tobytes(), np.float32).reshape(meters.shape)
        return cls(
            jpeg,
            meters,
            stamps[0],
            rgb.header.frame_id,
            tuple(received),
            (image.shape[1], image.shape[0]),
            (info.width, info.height),
            tuple(info.k),
            tuple(info.d),
            tuple(info.r),
            tuple(info.p),
            generation,
        )

    def surface_point(self, u, v):
        """Raw RGB pixel -> raw optical XYZ metres, or None near invalid/edges.

        Stereo publishes rectified axial Z. Transform raw pixels through K/D/R/P,
        sample its resized grid, then undo R before returning raw optical XYZ.
        The 3x3 neighborhood must be valid and agree within 1cm; even a passing
        sample does not certify stereo accuracy or clearance for robot motion.
        This retained snapshot can be old: callers must revalidate freshness,
        calibration and pose after inference, before acting on its coordinates.
        """
        import cv2

        w, h = self.image_size
        if not np.isfinite([u, v]).all() or not (0 <= u < w and 0 <= v < h):
            return None
        cw, ch = self.calibration_size
        k, r, p = np.reshape(self.k, (3, 3)), np.reshape(self.r, (3, 3)), np.reshape(self.p, (3, 4))
        raw = np.array([[[u * cw / w, v * ch / h]]], dtype=np.float64)
        rect = cv2.undistortPoints(raw, k, np.asarray(self.d), R=r, P=p).reshape(2)
        dh, dw = self.depth_m.shape
        if not np.isfinite(rect).all():
            return None
        x, y = np.rint(rect * [dw / cw, dh / ch]).astype(int)
        if not (1 <= x < dw - 1 and 1 <= y < dh - 1):
            return None
        patch = self.depth_m[y - 1 : y + 2, x - 1 : x + 2]
        if not np.isfinite(patch).all() or patch.min() <= 0 or np.ptp(patch) > 0.01:
            return None
        ray = np.linalg.solve(p[:, :3], [*rect, 1.0])
        point = r.T @ (ray * (float(np.median(patch)) / ray[2]))
        return tuple(float(value) for value in point)

    def surface_point_in_base(self, u, v):
        """Visible surface in capture-time base_link, never a motion target.

        This uses the measured TF at capture time. It does not rebase a retained
        point after the robot or object moves; obtain and validate a new snapshot.
        """
        pose = self.base_from_optical
        if pose is None or len(pose) != 7 or not np.isfinite(pose).all():
            return None
        q = np.asarray(pose[3:])
        if not np.isclose(q @ q, 1.0, atol=1e-5):
            return None
        point = self.surface_point(u, v)
        if point is None:
            return None
        point = np.asarray(point)
        rotated = point + 2 * np.cross(q[:3], np.cross(q[:3], point) + q[3] * point)
        return tuple(float(v) for v in rotated + pose[:3])
