# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Local ArUco pose and taught docking geometry. No world map or model calls."""

import json
import math
import time
from pathlib import Path

import cv2
import numpy as np
from innate_skills.follow_aruco import LOCK_CONFIRM_FRAMES, LOOP_PERIOD, LOST_GRACE_FRAMES, FollowAruco

from innate import vision
from innate.exceptions import SkillFailed
from innate.geometry import CX, CY, FX, FY, IMG_H, IMG_W, _cam_pose

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "box_marker.json"
DICTIONARY = "DICT_4X4_50"


def camera_matrix():
    return np.array([[FX, 0, CX], [0, FY, CY], [0, 0, 1]], dtype=float)


def camera_in_base(tilt):
    origin, forward, right, down = _cam_pose(tilt)
    transform = np.eye(4)
    transform[:3, :3] = np.array([right, down, forward]).T
    transform[:3, 3] = origin
    return transform


def rotation_distance(a, b):
    return math.acos(float(np.clip((np.trace(a[:3, :3].T @ b[:3, :3]) - 1) / 2, -1, 1)))


def validate_config(c):
    if c.get("version") not in (1, 2) or c.get("dictionary") != DICTIONARY:
        raise ValueError("Unsupported box marker configuration")
    if type(c.get("marker_id")) is not int or not 0 <= c["marker_id"] < 50:
        raise ValueError("Marker ID must be 0–49")
    if not 0.025 <= float(c["marker_size_m"]) <= 0.3:
        raise ValueError("Measure the printed black square: size must be 0.025–0.30m")
    if c.get("image_size") != [IMG_W, IMG_H]:
        raise ValueError("Camera resolution changed; reteach the box")
    if not np.allclose(np.asarray(c["camera_matrix"]), camera_matrix(), atol=1e-6):
        raise ValueError("Camera intrinsics changed; reteach the box")
    if not -40 <= float(c["head_tilt_deg"]) <= 0:
        raise ValueError("Invalid taught head angle")
    near = np.asarray(c["near_xy"], dtype=float)
    if near.shape != (2,) or not np.isfinite(near).all() or not (0.18 <= near[0] <= 0.3 and abs(near[1]) <= 0.1):
        raise ValueError("Drop coordinates outside the rehearsed envelope")
    pose = np.asarray(c["base_from_marker"], dtype=float)
    if (
        pose.shape != (4, 4)
        or not np.isfinite(pose).all()
        or not np.allclose(pose[3], [0, 0, 0, 1])
        or not np.allclose(pose[:3, :3].T @ pose[:3, :3], np.eye(3), atol=1e-5)
        or abs(np.linalg.det(pose[:3, :3]) - 1) > 1e-5
    ):
        raise ValueError("Invalid taught marker pose")
    return c


def load_config():
    try:
        c = validate_config(json.loads(CONFIG_PATH.read_text()))
        if c["version"] == 1:
            # First demo release incorrectly treated the teaching location as
            # the parked location. The observed tag is on the box's near face.
            c = front_tag_target(c)
        return c
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SkillFailed(f"Teach the box marker before using marker drop: {e}") from e


def front_tag_target(config):
    """Tag is rigidly mounted at the centre of the box's near/front face.

    Keep its measured mounting height/orientation, but place the desired robot
    at the known near-edge distance. Teaching can happen farther away.
    """
    c = dict(config)
    pose = np.array(c["base_from_marker"], dtype=float, copy=True)
    c["observed_base_from_marker"] = pose.tolist()
    pose[0, 3], pose[1, 3] = c["near_xy"]
    c["base_from_marker"] = pose.tolist()
    c["version"] = 2
    c["mount"] = "front_face_center"
    return validate_config(c)


class MarkerPose:
    def __init__(self, marker_id, size_m, tilt):
        if type(marker_id) is not int or not 0 <= marker_id < 50 or not 0.025 <= size_m <= 0.3:
            raise ValueError("Invalid marker ID or physical size")
        self.marker_id, self.tilt = marker_id, tilt
        s = size_m / 2
        # OpenCV IPPE_SQUARE order: top-left, top-right, bottom-right, bottom-left.
        self.object_points = np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], dtype=np.float32)
        self.detector = cv2.aruco.ArucoDetector(
            cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), cv2.aruco.DetectorParameters()
        )

    def detect_quad(self, gray):
        if gray is None or gray.shape != (IMG_H, IMG_W):
            return None
        corners, ids, _ = self.detector.detectMarkers(gray)
        matches = [] if ids is None else [q for q, i in zip(corners, ids.ravel(), strict=False) if i == self.marker_id]
        # Duplicate IDs cannot establish which box this is.
        if len(matches) != 1:
            return None
        return matches[0].reshape(4, 2)

    def detect(self, gray):
        quad = self.detect_quad(gray)
        return self.from_corners(quad) if quad is not None else None

    def from_corners(self, corners):
        corners = np.asarray(corners, dtype=np.float32)
        if corners.shape != (4, 2) or not np.isfinite(corners).all():
            return None
        if np.any(corners < 3) or np.any(corners[:, 0] >= IMG_W - 3) or np.any(corners[:, 1] >= IMG_H - 3):
            return None
        if min(np.linalg.norm(corners[i] - corners[(i + 1) % 4]) for i in range(4)) < 18:
            return None
        ok, rotations, translations, _ = cv2.solvePnPGeneric(
            self.object_points, corners, camera_matrix(), np.zeros(5), flags=cv2.SOLVEPNP_IPPE_SQUARE
        )
        if not ok:
            return None
        candidates = []
        for rvec, tvec in zip(rotations, translations, strict=False):
            if not np.isfinite(rvec).all() or not np.isfinite(tvec).all() or not 0.1 < tvec.ravel()[2] < 2:
                continue
            pose = np.eye(4)
            pose[:3, :3] = cv2.Rodrigues(rvec)[0]
            pose[:3, 3] = tvec.ravel()
            if np.any((self.object_points @ pose[:3, :3].T + pose[:3, 3])[:, 2] <= 0):
                continue
            projected = cv2.projectPoints(self.object_points, rvec, tvec, camera_matrix(), np.zeros(5))[0].reshape(4, 2)
            error = float(np.sqrt(np.mean(np.sum((projected - corners) ** 2, axis=1))))
            if error <= 1.5:
                candidates.append((error, camera_in_base(self.tilt) @ pose))
        candidates.sort(key=lambda p: p[0])
        if not candidates:
            return None
        if (
            len(candidates) > 1
            and candidates[1][0] - candidates[0][0] < 0.15
            and rotation_distance(candidates[0][1], candidates[1][1]) > math.radians(8)
        ):
            return None  # planar-pose ambiguity; wait for a better view
        return candidates[0][1]


def marker_features(quad):
    """Same center/mean side-size signal used by FollowAruco."""
    q = np.asarray(quad, dtype=float)
    if q.shape != (4, 2) or not np.isfinite(q).all():
        return None
    size = float(np.mean([np.linalg.norm(q[i] - q[(i + 1) % 4]) for i in range(4)]))
    if size < 12 or np.any(q < 2) or np.any(q[:, 0] >= IMG_W - 2) or np.any(q[:, 1] >= IMG_H - 2):
        return None
    return np.array([q[:, 0].mean(), q[:, 1].mean(), size])


class MarkerFollower:
    """Use the actual FollowAruco controller, not a separately tuned copy."""

    # Carrying a sock on the slippery demo floor: preserve the follower's
    # steering law, but bound both speeding up and slowing down.
    max_linear = 0.15
    max_reverse = 0.08
    max_angular = 0.6
    linear_slew = 0.2 / 3
    angular_slew = 0.8 / 3
    linear_braking = 0.4
    angular_braking = 2.0
    _smooth = staticmethod(FollowAruco._smooth)
    _send_cmd = FollowAruco._send_cmd
    _stop = FollowAruco._stop
    _drive_toward = FollowAruco._drive_toward

    def __init__(self, mobility):
        self.mobility = mobility
        self._frame_width = IMG_W
        self._offset_filtered = self._size_error_filtered = None
        self._cmd_linear = self._cmd_angular = 0.0
        self._last_cmd_time = None


class MarkerOvershootRecovery:
    """A clear side crossing triggers a bounded, raw-image turning correction."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.side = 0
        self.started = None
        self.centered = 0

    def update(self, offset, now):
        side = 1 if offset > 24 else -1 if offset < -24 else 0
        if self.started is None:
            if side and self.side and side != self.side:
                self.started = now
            elif side:
                self.side = side
        if self.started is None:
            return None
        if now - self.started > 5:
            raise SkillFailed("Box overshoot recovery timed out; stopped before release")
        if abs(offset) <= 8:
            self.centered += 1
            if self.centered >= 3:
                self.reset()
            return 0.0
        self.centered = 0
        # Positive image error means the box is right: command a right turn.
        # No filtered past-left error may override the observed correction.
        return max(-0.35, min(0.35, -1.5 * offset / (IMG_W / 2)))


class MarkerDock:
    """Image-based servo adapted from FollowAruco; no runtime pose fitting."""

    def __init__(self, host, config):
        self.host, self.config = host, validate_config(config)
        self.detector = MarkerPose(config["marker_id"], config["marker_size_m"], config["head_tilt_deg"])
        # Use teaching only to compute the desired image appearance. During
        # motion a decoded quadrilateral is sufficient; no PnP rejection gate.
        camera = np.linalg.inv(camera_in_base(config["head_tilt_deg"])) @ np.array(config["base_from_marker"])
        points = self.detector.object_points @ camera[:3, :3].T + camera[:3, 3]
        if np.any(points[:, 2] <= 0.05):
            raise SkillFailed("Taught tag is behind camera; reteach the front tag")
        pixels = points @ camera_matrix().T
        self.target_quad = pixels[:, :2] / pixels[:, 2:]
        self.goal = marker_features(self.target_quad)
        if self.goal is None:
            raise SkillFailed("Tag would leave the image at the drop pose; mount it higher and reteach")

    def _find_marker(self, follower):
        """Look around before driving, using only fresh decoded taught tags."""
        host = self.host
        raw = None
        start = last_frame = last_tick = time.monotonic()
        swept = 0.0
        lock = 0
        while time.monotonic() - start < 35:
            host.check_cancelled()
            now = time.monotonic()
            swept += abs(follower._cmd_angular) * min(now - last_tick, 0.4)
            last_tick = now
            image = host.main_image
            if not image or image is raw:
                if now - last_frame > 0.3:
                    follower._stop()
                    lock = 0
                if now - last_frame > 3:
                    raise SkillFailed("Camera stale while searching for box; stopped holding sock")
                host.sleep(LOOP_PERIOD)
                continue
            raw = image
            last_frame = now
            gray = vision.b64_to_gray(image)
            if gray is None:
                follower._stop()
                lock = 0
                host.sleep(LOOP_PERIOD)
                continue
            quad = self.detector.detect_quad(gray)
            if quad is not None and marker_features(quad) is not None:
                lock += 1
                follower._send_cmd(0.0, 0.0)
                if lock >= LOCK_CONFIRM_FRAMES and abs(follower._cmd_angular) <= 0.02:
                    follower._stop()
                    host.logger.info("[MarkerDock] Box marker found; starting approach")
                    return
            else:
                lock = 0
                if swept >= 2 * math.pi:
                    break
                # The same acceleration cap used for docking applies here.
                # Never translate while searching with a sock in the gripper.
                follower._send_cmd(0.0, 0.25)
            host.sleep(LOOP_PERIOD)
        raise SkillFailed("Box marker not found after looking around; stopped holding sock")

    def run(self, follower=None):
        host = self.host
        if follower is None:
            follower = MarkerFollower(host.mobility)
        raw = host.main_image
        start = last_seen = time.monotonic()
        lock = lost = stable = 0
        recovery = MarkerOvershootRecovery()
        try:
            self._find_marker(follower)
            raw = host.main_image
            start = last_seen = time.monotonic()
            while time.monotonic() - start < 30:
                host.check_cancelled()
                now = time.monotonic()
                image = host.main_image
                quad = None
                if image and image is not raw:
                    raw = image
                    quad = self.detector.detect_quad(vision.b64_to_gray(image))
                features = marker_features(quad) if quad is not None else None
                if features is None:
                    recovery.reset()
                    stable = 0
                    if lock < LOCK_CONFIRM_FRAMES:
                        lock = 0
                    lost += 1
                    follower._offset_filtered = follower._size_error_filtered = None
                    if lost > LOST_GRACE_FRAMES:
                        follower._stop()
                    else:
                        follower._send_cmd(0.0, 0.0)
                    if now - last_seen > 3:
                        raise SkillFailed("No fresh decoded box tag for 3 seconds; stopped before release")
                else:
                    last_seen = now
                    lost = 0
                    lock += 1
                    du, dv = features[:2] - self.goal[:2]
                    size_error = 1 - features[2] / self.goal[2]
                    if lock >= LOCK_CONFIRM_FRAMES:
                        correction = recovery.update(float(du), now)
                        if correction is not None:
                            stable = 0
                            follower._offset_filtered = follower._size_error_filtered = None
                            # Brake translation first, then turn in place using
                            # the existing gentle acceleration/strong braking.
                            follower._send_cmd(0.0, correction if abs(follower._cmd_linear) <= 0.005 else 0.0)
                            host.logger.info(
                                f"[MarkerDock] overshoot recovery offset={du:.1f}px "
                                f"requested_turn={correction:.3f} cmd=({follower._cmd_linear:.3f},"
                                f"{follower._cmd_angular:.3f})"
                            )
                            host.sleep(LOOP_PERIOD)
                            continue
                        if abs(du) <= 8 and abs(dv) <= 20 and abs(size_error) <= 0.06:
                            # Brake through the same slew limit before declaring
                            # arrival. Cancellation and marker loss still stop immediately.
                            follower._send_cmd(0.0, 0.0)
                            if abs(follower._cmd_linear) > 0.005 or abs(follower._cmd_angular) > 0.02:
                                stable = 0
                                host.sleep(LOOP_PERIOD)
                                continue
                            follower._stop()
                            stable += 1
                            if stable >= 3:
                                host.logger.info("[MarkerDock] FollowAruco approach aligned; ready to drop")
                                return tuple(self.config["near_xy"])
                        else:
                            stable = 0
                            follower._drive_toward(
                                quad,
                                target_center_x=self.goal[0],
                                target_size_frac=self.goal[2] / IMG_W,
                                size_deadband=0.04,
                            )
                    host.logger.info(f"[MarkerDock] follower offset=({du:.1f},{dv:.1f})px size_error={size_error:.3f}")
                host.sleep(LOOP_PERIOD)
            raise SkillFailed("Box tag visible but not aligned before approach timeout")
        finally:
            follower._stop()
