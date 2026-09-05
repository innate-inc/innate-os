#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Mobility - Provides base movement (wheels) control capabilities to skills.

This interface allows skills to:
1. Send velocity commands through the skills-priority cmd_vel mux input
2. Schedule automatic stop after a specified duration
3. Precise blocking rotation via Nav2
"""

import math
import time

from geometry_msgs.msg import PoseStamped, Twist
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from rclpy.node import Node
from rclpy.timer import Timer

from brain_client.common.logging import UniversalLogger
from brain_client.skills.types import SkillCancelled, cancellable_sleep


class Mobility:
    """High-level interface for base (wheel) motion.

    Injected as ``self.mobility`` when a skill declares ``mobility: Mobility``;
    the framework constructs it. Skills should use this instead of directly
    publishing directly to the base command topic.
    """

    def __init__(self, node: Node, logger, cmd_vel_topic: str = "/cmd_vel_skills"):
        self.node = node
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)
        self.cmd_vel_topic = cmd_vel_topic

        self._cmd_vel_pub = self.node.create_publisher(Twist, self.cmd_vel_topic, 10)
        self._stop_timer: Timer | None = None

        # Nav2 navigator for precise movements
        self._navigator = BasicNavigator(namespace="")
        self._navigator_mapfree = BasicNavigator(namespace="mapfree")

        self.logger.info(f"Mobility initialized with cmd_vel topic: {self.cmd_vel_topic}")

    def _schedule_stop(self, duration: float):
        # One timer, created once and retargeted per command. Destroying a
        # timer on a node a live executor is spinning races the executor's
        # wait set (InvalidHandle -> crash), and creating a fresh one per
        # deadman refresh (10-20/s) would leak cancelled timers in the node's
        # timer list — reset/cancel on a single timer avoids both.
        if duration is None or duration <= 0.0:
            return
        if self._stop_timer is None:
            self._stop_timer = self.node.create_timer(duration, self._on_stop_timer)
            return
        self._stop_timer.timer_period_ns = int(duration * 1e9)
        self._stop_timer.reset()

    def _on_stop_timer(self):
        if self._stop_timer is not None:
            self._stop_timer.cancel()  # one-shot: stays cancelled until the next _schedule_stop
        self._cmd_vel_pub.publish(Twist())
        self.logger.debug("Mobility: stop command published")

    def send_cmd_vel(
        self,
        linear_x: float = 0.0,
        angular_z: float = 0.0,
        duration: float | None = None,
    ) -> None:
        """Publish a Twist on the cmd_vel topic.

        Args:
            linear_x: Linear velocity in m/s.
            angular_z: Angular velocity in rad/s.
            duration: If provided (>0), schedule a stop command after this many seconds.
        """
        twist = Twist()
        twist.linear.x = float(linear_x)
        twist.angular.z = float(angular_z)

        self._cmd_vel_pub.publish(twist)

        self.logger.debug(
            f"Mobility: cmd_vel published (linear_x={linear_x}, angular_z={angular_z}, duration={duration})"
        )

        if duration is not None and duration > 0.0:
            self._schedule_stop(duration)
        elif self._stop_timer is not None:
            # Continuous motion requested: disarm any stop still pending from
            # an earlier timed command so it doesn't cut this one short.
            self._stop_timer.cancel()

    def rotate_in_place(self, angular_speed: float, duration: float) -> None:
        """Rotate in place with specified angular speed for a duration (non-blocking).

        Args:
            angular_speed: Angular velocity in rad/s (sign determines direction).
            duration: Duration in seconds, after which a stop command is sent.
        """
        self.send_cmd_vel(linear_x=0.0, angular_z=angular_speed, duration=duration)

    def rotate(self, angle_radians: float) -> bool:
        """Rotate in place by a specific angle using Nav2 (blocking).

        Args:
            angle_radians: Angle to rotate in radians (positive = counter-clockwise).

        Returns:
            bool: True if rotation succeeded, False otherwise.
        """
        goal_pose = PoseStamped()
        goal_pose.header.frame_id = "base_link"
        goal_pose.header.stamp = self._navigator.get_clock().now().to_msg()
        goal_pose.pose.position.x = 0.0
        goal_pose.pose.position.y = 0.0
        goal_pose.pose.position.z = 0.0
        goal_pose.pose.orientation.x = 0.0
        goal_pose.pose.orientation.y = 0.0
        goal_pose.pose.orientation.z = math.sin(angle_radians / 2.0)
        goal_pose.pose.orientation.w = math.cos(angle_radians / 2.0)

        self.logger.info(f"Mobility: rotating {math.degrees(angle_radians):.1f}° via Nav2")

        # Get path to verify it's possible
        path = self._navigator_mapfree.getPath(goal_pose, goal_pose, use_start=False)
        if path is None:
            self.logger.error("Mobility: failed to get rotation path")
            return False

        # Execute rotation (blocking)
        self._navigator.goToPose(goal_pose, behavior_tree="mapfree")

        while not self._navigator.isTaskComplete():
            try:
                cancellable_sleep(0.1)
            except SkillCancelled:
                self._navigator.cancelTask()
                raise

        result = self._navigator.getResult()
        if result == TaskResult.SUCCEEDED:
            self.logger.info("Mobility: rotation complete")
            return True
        else:
            self.logger.error(f"Mobility: rotation failed with {result}")
            return False

    # --- base primitives ---
    # Skills call these as methods: self.mobility.rotate_by(...), .drive(...).

    def stop(self):
        self.send_cmd_vel(0.0, 0.0, 0.1)

    halt = stop  # the framework brake (Skill._halt_interfaces)

    @staticmethod
    def odom_xyt(odom):
        """(x, y, theta) from nested dict or flat dataclass, or None."""
        if odom is None:
            return None
        try:
            if isinstance(odom, dict):
                p = odom["pose"]["pose"]["position"]
                return (float(p["x"]), float(p["y"]), math.radians(float(odom["theta_degrees"])))
            return (float(odom.x), float(odom.y), float(odom.theta))
        except (KeyError, AttributeError, TypeError):
            return None

    @staticmethod
    def servo_vel(err_px, gain, v_min, v_max, deadband_px):
        """Pixel P-servo axis: gain per 100 px, clamp to [v_min, v_max], 0 in deadband."""
        if abs(err_px) <= deadband_px:
            return 0.0
        v = max(-v_max, min(v_max, -gain / 100.0 * err_px))
        return math.copysign(v_min, v) if abs(v) < v_min else v

    def rotate_by(
        self, get_xyt, angle, *, kp=1.2, wz_max=0.5, wz_min=0.15, tolerance=math.radians(2.5), timeout=12.0, logger=None
    ):
        """Rotate in place by `angle` rad (positive = counter-clockwise),
        closed on odometry yaw (open-loop if get_xyt yields None). Returns
        True once the yaw error is within `tolerance` (or open-loop best
        effort), False on timeout or odometry loss."""
        logger = logger or self.logger
        try:
            xyt = get_xyt()
            if xyt is None:
                logger.warning("[mobility] no odom — open-loop rotate")
                duration = abs(angle) / 0.35
                self.send_cmd_vel(0.0, math.copysign(0.35, angle), duration)
                cancellable_sleep(duration + 0.4)
                return True
            target = xyt[2] + angle
            err = math.atan2(math.sin(angle), math.cos(angle))
            t0 = time.time()
            while time.time() - t0 < timeout:
                xyt = get_xyt()
                if xyt is None:
                    logger.warning("[mobility] odom lost mid-rotate — stopping short")
                    return False
                err = math.atan2(math.sin(target - xyt[2]), math.cos(target - xyt[2]))
                if abs(err) < tolerance:
                    return True
                wz = max(-wz_max, min(wz_max, kp * err))
                if abs(wz) < wz_min:
                    wz = math.copysign(wz_min, wz)
                self.send_cmd_vel(0.0, wz, 0.15)
                cancellable_sleep(0.08)
            logger.warning(
                f"[mobility] rotate_by timed out after {timeout}s "
                f"({math.degrees(angle):.1f}° requested, {math.degrees(err):.1f}° short)"
            )
            return False
        finally:
            self.stop()

    def drive(self, get_xyt, dist, *, kp=0.3, v_max=0.10, v_min=0.04, tolerance=0.015, timeout=15.0, logger=None):
        """Drive straight by `dist` m (negative = backwards), closed on
        odometry position (open-loop if get_xyt yields None). Returns True
        once the remaining distance is within `tolerance` (a `dist` under
        `tolerance` is a no-op), False on timeout or odometry loss."""
        logger = logger or self.logger
        if abs(dist) < tolerance:
            return True
        try:
            xyt = get_xyt()
            if xyt is None:
                logger.warning("[mobility] no odom — open-loop drive")
                duration = abs(dist) / 0.08
                self.send_cmd_vel(math.copysign(0.08, dist), 0.0, duration)
                cancellable_sleep(duration + 0.4)
                return True
            x0, y0 = xyt[0], xyt[1]
            err = abs(dist)
            t0 = time.time()
            while time.time() - t0 < timeout:
                xyt = get_xyt()
                if xyt is None:
                    logger.warning("[mobility] odom lost mid-drive — stopping short")
                    return False
                err = abs(dist) - math.hypot(xyt[0] - x0, xyt[1] - y0)
                if err < tolerance:
                    return True
                v = math.copysign(max(v_min, min(v_max, kp * err)), dist)
                self.send_cmd_vel(v, 0.0, 0.15)
                cancellable_sleep(0.08)
            logger.warning(f"[mobility] drive timed out after {timeout}s ({dist:.2f} m requested, {err:.2f} m short)")
            return False
        finally:
            self.stop()
