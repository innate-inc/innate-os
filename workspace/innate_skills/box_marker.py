# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Local ArUco pose and taught docking geometry. No world map or model calls."""

import json
import math
import time
from pathlib import Path

import cv2
import numpy as np
from innate_skills.follow_aruco import LOOP_PERIOD, LOST_GRACE_FRAMES, FollowAruco

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
    if near.shape != (2,) or not np.isfinite(near).all() or not (0.14 <= near[0] <= 0.3 and abs(near[1]) <= 0.1):
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


def marker_image(host):
    """Use opt-in native detail; older drivers and simulation keep their normal feed."""
    return getattr(host, "main_highres_image", None) or host.main_image


class MarkerPose:
    def __init__(self, marker_id, size_m, tilt):
        if type(marker_id) is not int or not 0 <= marker_id < 50 or not 0.025 <= size_m <= 0.3:
            raise ValueError("Invalid marker ID or physical size")
        self.marker_id, self.tilt = marker_id, tilt
        s = size_m / 2
        # OpenCV IPPE_SQUARE order: top-left, top-right, bottom-right, bottom-left.
        self.object_points = np.array([[-s, s, 0], [s, s, 0], [s, -s, 0], [-s, -s, 0]], dtype=np.float32)
        parameters = cv2.aruco.DetectorParameters()
        # AprilTag-based quad extraction handles angled views our default
        # contour detector misses; the printed tag is still DICT_4X4_50.
        parameters.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_APRILTAG
        self.detector = cv2.aruco.ArucoDetector(
            cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), parameters
        )

    def detect_quad(self, gray):
        self.last_diagnostic = {"reason": "invalid_image"}
        if gray is None or gray.ndim != 2 or not all(gray.shape):
            return None
        source_height, source_width = gray.shape
        corners, ids, _ = self.detector.detectMarkers(gray)
        # More pixels do not always produce a better quad at grazing angles.
        # Retry the same frame at the established resolution if native decoding
        # misses the taught ID. Preserve duplicate-ID rejection at either size.
        used_height, used_width = source_height, source_width
        if (source_height, source_width) != (IMG_H, IMG_W) and (ids is None or self.marker_id not in ids):
            standard = cv2.resize(gray, (IMG_W, IMG_H), interpolation=cv2.INTER_AREA)
            corners, ids, _ = self.detector.detectMarkers(standard)
            used_height, used_width = IMG_H, IMG_W
        matches = [] if ids is None else [q for q, i in zip(corners, ids.ravel(), strict=False) if i == self.marker_id]
        self.last_diagnostic = {
            "reason": "marker_missing" if not matches else "duplicate_marker" if len(matches) > 1 else "quad_found",
            "seen_ids": [] if ids is None else ids.ravel().tolist(),
            "matches": len(matches),
        }
        # Duplicate IDs cannot establish which box this is.
        if len(matches) != 1:
            return None
        # Pose, overlays and steering retain the calibrated 640x480 coordinates.
        quad = matches[0].reshape(4, 2).astype(np.float32)
        quad *= np.array([IMG_W / used_width, IMG_H / used_height], dtype=np.float32)
        self.last_diagnostic["input_size"] = [int(source_width), int(source_height)]
        self.last_diagnostic["detection_size"] = [int(used_width), int(used_height)]
        return quad

    def detect(self, gray):
        quad = self.detect_quad(gray)
        if quad is None:
            return None
        width, height = self.last_diagnostic["detection_size"]
        return self.from_corners(quad, detection_scale=(width / IMG_W, height / IMG_H))

    def from_corners(self, corners, *, detection_scale=(1.0, 1.0)):
        self.last_diagnostic = {"reason": "invalid_corners"}
        corners = np.asarray(corners, dtype=np.float32)
        if corners.shape != (4, 2) or not np.isfinite(corners).all():
            return None
        self.last_diagnostic.update(corners_px=corners.tolist(), reason="edge_clipped")
        if np.any(corners < 3) or np.any(corners[:, 0] >= IMG_W - 3) or np.any(corners[:, 1] >= IMG_H - 3):
            return None
        # The resolution gate concerns observed pixels, not the normalized
        # calibration coordinates used by PnP and steering.
        observed = corners * np.asarray(detection_scale)
        min_side = min(np.linalg.norm(observed[i] - observed[(i + 1) % 4]) for i in range(4))
        self.last_diagnostic.update(reason="marker_too_small", min_side_px=float(min_side))
        if min_side < 18:
            return None
        ok, rotations, translations, _ = cv2.solvePnPGeneric(
            self.object_points, corners, camera_matrix(), np.zeros(5), flags=cv2.SOLVEPNP_IPPE_SQUARE
        )
        self.last_diagnostic.update(reason="pnp_failed")
        if not ok:
            return None
        candidates = []
        details = []
        self.last_diagnostic.update(reason="no_valid_pose", candidates=details)
        for rvec, tvec in zip(rotations, translations, strict=False):
            detail = {"depth_m": float(tvec.ravel()[2]), "reason": "invalid_or_out_of_range_depth"}
            details.append(detail)
            if not np.isfinite(rvec).all() or not np.isfinite(tvec).all() or not 0.1 < tvec.ravel()[2] < 2:
                continue
            pose = np.eye(4)
            pose[:3, :3] = cv2.Rodrigues(rvec)[0]
            pose[:3, 3] = tvec.ravel()
            detail["reason"] = "corners_behind_camera"
            if np.any((self.object_points @ pose[:3, :3].T + pose[:3, 3])[:, 2] <= 0):
                continue
            projected = cv2.projectPoints(self.object_points, rvec, tvec, camera_matrix(), np.zeros(5))[0].reshape(4, 2)
            error = float(np.sqrt(np.mean(np.sum((projected - corners) ** 2, axis=1))))
            detail.update(reprojection_px=error, reason="accepted" if error <= 7.0 else "reprojection_error")
            if error <= 7.0:
                candidates.append((error, camera_in_base(self.tilt) @ pose))
        candidates.sort(key=lambda p: p[0])
        if not candidates:
            return None
        if len(candidates) > 1:
            self.last_diagnostic.update(
                error_gap_px=candidates[1][0] - candidates[0][0],
                rotation_gap_deg=math.degrees(rotation_distance(candidates[0][1], candidates[1][1])),
            )
        if (
            len(candidates) > 1
            and candidates[1][0] - candidates[0][0] < 0.15
            and rotation_distance(candidates[0][1], candidates[1][1]) > math.radians(8)
        ):
            self.last_diagnostic["reason"] = "ambiguous_planar_pose"
            return None  # planar-pose ambiguity; wait for a better view
        self.last_diagnostic["reason"] = "accepted"
        return candidates[0][1]


def box_release_target(pose, right_m=0.08, inward_m=0.12):
    """Metric floor-plane offsets from a tag on the box's front face.

    The tag's outward normal is +Z regardless of how the print is rotated
    on that face. Derive box-right from the horizontal inward normal rather
    than the printed tag's X axis (the demo tag is mounted rotated).
    """
    pose = np.asarray(pose, dtype=float)
    if pose.shape != (4, 4) or not np.isfinite(pose).all():
        raise SkillFailed("Invalid live box marker pose")
    inward = -pose[:2, 2]
    length = np.linalg.norm(inward)
    if length < 0.7:
        raise SkillFailed("Box marker is not on a near-vertical front face")
    inward = inward / length
    origin = pose[:2, 3]
    if np.dot(inward, origin) <= 0:
        raise SkillFailed("Box marker orientation faces away from the robot")
    right = np.array([inward[1], -inward[0]])
    target = origin + inward_m * inward + right_m * right
    return target, math.atan2(inward[1], inward[0])


def observe_box_release(host, detector, right_m, inward_m, *, return_heading=False, recover_right=False):
    """Measure a fresh pose; optionally recover arm occlusion before release."""
    host.mobility.stop()
    host.sleep(0.15)
    raw = marker_image(host)
    last_fresh = time.monotonic()
    recovery = MarkerFollower(host.mobility) if recover_right else None
    if recovery is not None:
        recovery.max_angular = 0.4
        recovery.angular_slew = 6.0
    turning = False
    try:
        while True:
            host.check_cancelled()
            image = marker_image(host)
            if image and image is not raw:
                raw = image
                last_fresh = time.monotonic()
                pose = detector.detect(vision.b64_to_gray(image))
                if pose is not None:
                    if recovery is not None:
                        recovery._stop()
                    if turning:
                        host.logger.info("[BoxFrame] release tag reacquired; stopping and discarding moving pose")
                        # The detected pose belongs to the rotating base. Stop,
                        # settle, then require a frame newer than the settling
                        # interval before using robot-relative drop coordinates.
                        host.sleep(0.20)
                        raw = marker_image(host)
                        last_fresh = time.monotonic()
                        turning = False
                        continue
                    target, yaw = box_release_target(pose, right_m, inward_m)
                    host.logger.info(
                        f"[BoxFrame] fresh pose target=({target[0]:.3f},{target[1]:.3f})m "
                        f"yaw={math.degrees(yaw):.1f}deg"
                    )
                    if return_heading:
                        return float(target[0]), float(target[1]), yaw
                    return float(target[0]), float(target[1])
                if recovery is not None:
                    missing = getattr(detector, "last_diagnostic", {}).get("reason") == "marker_missing"
                    if missing:
                        if not turning:
                            host.logger.info("[BoxFrame] release tag missing; recovering right at up to 0.40rad/s, no translation")
                        recovery._send_cmd(0.0, -0.40)
                        turning = True
                    else:
                        recovery._stop()
                host.logger.info(f"[BoxFrame] waiting for usable pose: {getattr(detector, 'last_diagnostic', {})}")
            elif time.monotonic() - last_fresh > 3:
                raise SkillFailed("Camera stale while estimating box pose")
            elif recovery is not None and time.monotonic() - last_fresh > 0.3:
                # Match docking: stop rotation when camera updates stall.
                recovery._stop()
            host.sleep(0.05)

    finally:
        if recovery is not None:
            recovery._stop()


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

    def fast_approach(self):
        """Reach approach speeds promptly, keeping independent braking limits."""
        self.linear_slew = 1.0
        self.angular_slew = 6.0

    def final_approach(self):
        """Short, eased docking motion on the slippery demo floor."""
        self.max_linear = 0.10
        self.max_reverse = 0.04
        self.max_angular = 0.60
        self.min_forward = 0.04
        self.linear_slew = 0.15
        self.linear_braking = 0.40
        self.angular_slew = 1.2
        self.angular_braking = 2.0

    def __init__(self, mobility):
        self.mobility = mobility
        self._frame_width = IMG_W
        self._offset_filtered = self._size_error_filtered = None
        self._cmd_linear = self._cmd_angular = 0.0
        self._last_cmd_time = None


class MarkerOvershootRecovery:
    """Large image offsets trigger a brief raw-image turning correction."""

    def __init__(self, center_tolerance=24):
        self.center_tolerance = center_tolerance
        self.reset()

    def reset(self):
        self.side = 0
        self.started = None
        self.centered = 0

    def update(self, offset, now):
        if self.started is None and abs(offset) > 64:
            self.started = now
        if self.started is None:
            return None
        if abs(offset) <= self.center_tolerance:
            self.centered += 1
            if self.centered >= 1:
                self.reset()
            return 0.0
        self.centered = 0
        # Positive image error means the box is right: command a right turn.
        # No filtered past-left error may override the observed correction.
        speed = max(0.4, min(0.6, 3.0 * abs(offset) / (IMG_W / 2)))
        return -speed if offset > 0 else speed


class MarkerDock:
    """Image-based servo adapted from FollowAruco; no runtime pose fitting."""

    horizontal_tolerance = 8
    size_tolerance = 0.06
    search_speed = -0.5  # box is right of the sock pile; negative yaw turns right
    stationary = False
    search_before_approach = True
    approach_timeout = 30
    search_timeout = 35

    def __init__(self, host, config):
        self.host, self.config = host, validate_config(config)
        self.detector = MarkerPose(config["marker_id"], config["marker_size_m"], config["head_tilt_deg"])
        # Use the taught geometry for apparent size (distance), not steering.
        # During motion a decoded quadrilateral is sufficient; no PnP gate.
        camera = np.linalg.inv(camera_in_base(config["head_tilt_deg"])) @ np.array(config["base_from_marker"])
        points = self.detector.object_points @ camera[:3, :3].T + camera[:3, 3]
        if np.any(points[:, 2] <= 0):
            # Teaching no longer vetoes docking through projected visibility.
            # Fall back to a front-facing metric size at the configured distance.
            size = float((FX + FY) / 2 * config["marker_size_m"] / config["near_xy"][0])
            self.target_quad = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]]) * size / 2
            self.target_quad += [IMG_W / 2, IMG_H / 2]
        else:
            pixels = points @ camera_matrix().T
            self.target_quad = pixels[:, :2] / pixels[:, 2:]
        q = self.target_quad
        size = float(np.mean([np.linalg.norm(q[i] - q[(i + 1) % 4]) for i in range(4)]))
        self.goal = np.array([q[:, 0].mean(), q[:, 1].mean(), size])
        # Center the tag in the camera image regardless of where it was taught.
        # Translate the reference quad without changing its size/distance target.
        # The arm's box-relative release offset is handled separately.
        self.target_quad[:, 0] += IMG_W / 2 - self.goal[0]
        self.goal[0] = IMG_W / 2

    def _sleep_cycle(self, started):
        # Include decoding/detection/control work in the 100 ms cycle budget.
        self.host.sleep(max(0.0, LOOP_PERIOD - (time.monotonic() - started)))

    def _find_marker(self, follower):
        """Look around before driving, using only fresh decoded taught tags."""
        host = self.host
        raw = None
        start = last_frame = last_tick = time.monotonic()
        swept = 0.0
        while time.monotonic() - start < self.search_timeout:
            cycle_started = time.monotonic()
            host.check_cancelled()
            now = time.monotonic()
            swept += abs(follower._cmd_angular) * min(now - last_tick, 0.4)
            last_tick = now
            image = marker_image(host)
            if not image or image is raw:
                if now - last_frame > 0.3:
                    follower._stop()
                if now - last_frame > 3:
                    raise SkillFailed("Camera stale while searching for box; stopped holding sock")
                self._sleep_cycle(cycle_started)
                continue
            raw = image
            last_frame = now
            gray = vision.b64_to_gray(image)
            if gray is None:
                follower._stop()
                self._sleep_cycle(cycle_started)
                continue
            quad = self.detector.detect_quad(gray)
            if quad is not None and marker_features(quad) is not None:
                follower._stop()
                host.logger.info("[MarkerDock] Box marker found on first valid detection; starting approach")
                return
            else:
                if swept >= 2 * math.pi:
                    break
                # The same acceleration cap used for docking applies here.
                # Never translate while searching with a sock in the gripper.
                follower._send_cmd(0.0, self.search_speed)
            self._sleep_cycle(cycle_started)
        raise SkillFailed("Box marker not found after looking around; stopped holding sock")

    def _recover_close_marker(self, follower):
        """Turn right to clear arm occlusion, without translating toward the box."""
        host = self.host
        follower._stop()
        raw = marker_image(host)
        started = last_fresh = time.monotonic()
        old_max, old_slew = follower.max_angular, follower.angular_slew
        follower.max_angular, follower.angular_slew = 0.4, 6.0
        host.logger.info("[MarkerDock] close tag lost; recovering right at up to 0.40rad/s, no translation")
        try:
            while time.monotonic() - started < self.approach_timeout:
                cycle_started = time.monotonic()
                host.check_cancelled()
                image = marker_image(host)
                now = time.monotonic()
                if image and image is not raw:
                    raw = image
                    last_fresh = now
                    if self.detector.detect_quad(vision.b64_to_gray(image)) is not None:
                        host.logger.info("[MarkerDock] close tag reacquired; stopping right turn")
                        return
                    follower._send_cmd(0.0, -0.4)
                elif now - last_fresh > 0.3:
                    follower._stop()
                self._sleep_cycle(cycle_started)
            raise SkillFailed("Tag still missing after approach recovery timeout; holding position")
        finally:
            follower._stop()
            follower.max_angular, follower.angular_slew = old_max, old_slew

    def run(self, follower=None):
        host = self.host
        if follower is None:
            follower = MarkerFollower(host.mobility)
        raw = marker_image(host)
        start = last_seen = time.monotonic()
        lock = lost = 0
        last_offset = None
        near_box = arrival_latched = False
        recovery = MarkerOvershootRecovery(24 if not self.stationary else self.horizontal_tolerance)
        try:
            if self.search_before_approach:
                self._find_marker(follower)
            raw = marker_image(host)
            start = last_seen = time.monotonic()
            while time.monotonic() - start < self.approach_timeout:
                cycle_started = time.monotonic()
                host.check_cancelled()
                now = time.monotonic()
                if arrival_latched:
                    follower._send_cmd(0.0, 0.0)
                    if abs(follower._cmd_linear) <= 0.005 and abs(follower._cmd_angular) <= 0.02:
                        follower._stop()
                        host.logger.info("[MarkerDock] arrival latched and braking complete; ready to drop")
                        return tuple(self.config["near_xy"])
                    self._sleep_cycle(cycle_started)
                    continue
                image = marker_image(host)
                quad = None
                fresh_frame = bool(image and image is not raw)
                if fresh_frame:
                    raw = image
                    quad = self.detector.detect_quad(vision.b64_to_gray(image))
                features = marker_features(quad) if quad is not None else None
                if features is None:
                    if fresh_frame and last_offset is not None and near_box:
                        if self.stationary:
                            follower._stop()
                            raise SkillFailed("Close tag lost; holding position without rotating")
                        self._recover_close_marker(follower)
                        last_seen = time.monotonic()
                        lock = lost = 0
                        recovery.reset()
                        continue
                    if fresh_frame and last_offset is not None:
                        # Brake, then reacquire toward the last visible side.
                        # Reuse the existing search, including camera freshness
                        # handling, Stop and its bounded full-turn search.
                        follower._stop()
                        self._sleep_cycle(cycle_started)
                        previous_search_speed = self.search_speed
                        self.search_speed = -min(abs(previous_search_speed), follower.max_angular) if last_offset > 0 else min(abs(previous_search_speed), follower.max_angular)
                        try:
                            self._find_marker(follower)
                        finally:
                            self.search_speed = previous_search_speed
                        last_seen = time.monotonic()
                        lock = lost = 0
                        last_offset = None
                        raw = marker_image(host)
                        continue
                    recovery.reset()
                    if lock < 1:
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
                    last_offset = float(du)
                    size_error = 1 - features[2] / self.goal[2]
                    near_box = near_box or size_error <= 0.25
                    if lock >= 1:
                        arrived = (self.stationary and abs(du) <= self.horizontal_tolerance) or (
                            not self.stationary and size_error <= 0.04
                        )
                        # Distance arrival wins over optional centering recovery.
                        correction = None if arrived else recovery.update(float(du), now)
                        if correction is not None:
                            follower._offset_filtered = follower._size_error_filtered = None
                            # Brake translation first, then turn in place using
                            # the existing gentle acceleration/strong braking.
                            follower._send_cmd(0.0, correction if abs(follower._cmd_linear) <= 0.005 else 0.0)
                            host.logger.info(
                                f"[MarkerDock] overshoot recovery offset={du:.1f}px "
                                f"requested_turn={correction:.3f} cmd=({follower._cmd_linear:.3f},"
                                f"{follower._cmd_angular:.3f})"
                            )
                            self._sleep_cycle(cycle_started)
                            continue
                        if arrived:
                            arrival_latched = True
                            host.logger.info(f"[MarkerDock] arrival latched offset={du:.1f}px size_error={size_error:.3f}; braking")
                            follower._send_cmd(0.0, 0.0)
                        else:
                            follower._drive_toward(
                                quad,
                                target_center_x=self.goal[0],
                                target_size_frac=self.goal[2] / IMG_W,
                                size_deadband=0.04,
                                min_forward=0.0 if self.stationary else getattr(follower, "min_forward", 0.06),
                            )
                    host.logger.info(f"[MarkerDock] follower offset=({du:.1f},{dv:.1f})px size_error={size_error:.3f}")
                self._sleep_cycle(cycle_started)
            raise SkillFailed("Box tag visible but not aligned before approach timeout")
        finally:
            follower._stop()
