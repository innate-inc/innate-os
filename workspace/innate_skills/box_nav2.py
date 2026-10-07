# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Direct base control to a box-front point, then visual docking; no Nav2."""

import math
import time

import numpy as np
from innate_skills.box_marker import MarkerDock, MarkerFollower, observe_box_release

from innate.exceptions import SkillFailed


def wrap(angle):
    return math.atan2(math.sin(angle), math.cos(angle))


def approach_goal(marker_xy, yaw):
    """Destination 26 cm in front of the tag, with travel-facing heading."""
    marker = np.asarray(marker_xy, dtype=float)
    if marker.shape != (2,) or not np.isfinite(marker).all() or not math.isfinite(yaw):
        raise SkillFailed("Invalid box pose for approach")
    goal = marker - 0.26 * np.array([math.cos(yaw), math.sin(yaw)])
    return float(goal[0]), float(goal[1]), math.atan2(goal[1], goal[0])


def in_odom(points, origin):
    x, y, yaw = origin
    c, s = math.cos(yaw), math.sin(yaw)
    return [(x + c * px - s * py, y + s * px + c * py, wrap(yaw + a)) for px, py, a in points]


class PointController:
    """Direct proportional distance/heading control with FollowAruco slew limits."""

    def __init__(self, host):
        self.host = host
        self.follower = MarkerFollower(host.mobility)
        self.follower.max_linear = 0.30
        self.follower.max_angular = 0.90
        self.follower.fast_approach()
        self.follower.linear_braking = 0.40
        self.follower.angular_braking = 1.80

    def fresh_odom(self):
        odom = self.host.odom
        if (odom is None or odom.frame_id != "odom"
                or not all(math.isfinite(v) for v in (odom.x, odom.y, odom.theta, odom.stamp))
                or not 0 <= time.time() - odom.stamp < 0.5):
            raise SkillFailed("Fresh odometry in odom frame required for box approach")
        return odom

    def turn(self, yaw):
        try:
            while True:
                self.host.check_cancelled()
                error = wrap(yaw - self.fresh_odom().theta)
                if abs(error) <= 0.12:
                    self.follower._send_cmd(0.0, 0.0)
                    if abs(self.follower._cmd_angular) <= 0.02:
                        return
                else:
                    speed = min(self.follower.max_angular, 3.0 * abs(error),
                                math.sqrt(2 * self.follower.angular_braking * max(0, abs(error) - 0.12)))
                    self.follower._send_cmd(0.0, math.copysign(speed, error))
                self.host.sleep(0.05)
        finally:
            self.follower._stop()

    def drive(self, destination):
        x, y, _ = destination
        try:
            odom = self.fresh_odom()
            if math.hypot(x - odom.x, y - odom.y) <= 0.04:
                return
            self.turn(math.atan2(y - odom.y, x - odom.x))
            self.host.sleep(0.15)
            last_log = -math.inf
            while True:
                self.host.check_cancelled()
                odom = self.fresh_odom()
                distance = math.hypot(x - odom.x, y - odom.y)
                if distance <= 0.04:
                    self.follower._send_cmd(0.0, 0.0)
                    if abs(self.follower._cmd_linear) <= 0.005 and abs(self.follower._cmd_angular) <= 0.02:
                        return
                    self.host.sleep(0.05)
                    continue
                error = wrap(math.atan2(y - odom.y, x - odom.x) - odom.theta)
                # Turn back toward the destination if knocked off heading;
                # slow smoothly near the point instead of orbiting past it.
                linear = min(self.follower.max_linear, 2.0 * distance,
                             math.sqrt(2 * self.follower.linear_braking * max(0, distance - 0.04))) * max(0.0, math.cos(error)) ** 4
                angular = max(-self.follower.max_angular, min(self.follower.max_angular, 2.0 * error))
                self.follower._send_cmd(linear, angular)
                if time.monotonic() - last_log >= 0.5:
                    self.host.logger.info(
                        f"[BoxApproach] direct distance={distance:.3f}m heading_error={math.degrees(error):.1f}deg "
                        f"command=({self.follower._cmd_linear:.3f},{self.follower._cmd_angular:.3f})"
                    )
                    last_log = time.monotonic()
                self.host.sleep(0.05)
        finally:
            self.follower._stop()

    def close(self):
        self.follower._stop()


def dock_via_point(host, config):
    dock = MarkerDock(host, config)
    follower = MarkerFollower(host.mobility)
    follower.fast_approach()
    follower.max_angular = 0.70
    dock.search_speed = -0.70
    dock.search_timeout = 175
    try:
        dock._find_marker(follower)
    finally:
        follower._stop()
    session = PointController(host)
    try:
        x, y, yaw = observe_box_release(host, dock.detector, 0.0, 0.0, return_heading=True)
        # Compare the tag normal with the robot-to-tag ray, not robot heading.
        # Rotating in place changes both bearings equally, not our frontality.
        bearing_to_tag = math.atan2(y, x)
        approach_angle = wrap(yaw - bearing_to_tag)
        host.logger.info(
            f"[BoxApproach] tag_yaw={math.degrees(yaw):.1f}deg "
            f"tag_bearing={math.degrees(bearing_to_tag):.1f}deg "
            f"approach_angle={math.degrees(approach_angle):.1f}deg"
        )
        if abs(approach_angle) <= math.radians(20):
            host.logger.info("[BoxApproach] within 20deg of tag perpendicular; direct visual docking")
            return
        odom = session.fresh_odom()
        origin = (odom.x, odom.y, odom.theta)
        marker_odom = in_odom([(x, y, yaw)], origin)[0]
        destination = in_odom([approach_goal((x, y), yaw)], origin)[0]
        host.logger.info(
            f"[BoxApproach] direct marker=({x:.3f},{y:.3f}) "
            f"tag_yaw={math.degrees(yaw):.1f}deg destination={destination}"
        )
        session.drive(destination)
        # Finish translation before turning toward the tag.
        host.mobility.stop()
        host.sleep(0.15)
        odom = session.fresh_odom()
        bearing = math.atan2(marker_odom[1] - odom.y, marker_odom[0] - odom.x)
        host.logger.info(f"[BoxApproach] stopped; turning in place toward tag at {math.degrees(bearing):.1f}deg")
        session.turn(bearing)

    finally:
        session.close()
