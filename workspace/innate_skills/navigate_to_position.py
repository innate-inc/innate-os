# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import math
import threading
import time

import rclpy
from geometry_msgs.msg import PoseStamped, Twist
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from rclpy.duration import Duration
from rclpy.qos import DurabilityPolicy, QoSProfile
from rclpy.time import Time
from tf2_ros import TransformException
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener

from brain_client.common.geometry import quaternion_to_yaw
from brain_client.skills.types import Skill, SkillResult

# Frame local (robot-relative) goals are resolved into before being sent to
# Nav2. Must match the mapfree costmap's global_frame (mars_nav costmap.yaml).
LOCAL_GOAL_FIXED_FRAME = "odom"


def resolve_local_goal(base_x, base_y, base_yaw, x, y, theta):
    """Compose a base_link-relative (x, y, theta) goal with the robot's pose in
    the fixed frame, returning (gx, gy, gyaw) expressed in that fixed frame."""
    gx = base_x + x * math.cos(base_yaw) - y * math.sin(base_yaw)
    gy = base_y + x * math.sin(base_yaw) + y * math.cos(base_yaw)
    return gx, gy, base_yaw + theta


class Nav2Controller:
    def __init__(self, logger, primitive):
        """
        Initialize the Nav2Controller by creating a BasicNavigator instance
        """
        # Create a BasicNavigator instance to communicate with Nav2.
        self.navigator = BasicNavigator(namespace="")
        self.navigator_mapfree = BasicNavigator(namespace="mapfree")
        self.navigator_navigation = BasicNavigator(namespace="navigation")
        self.logger = logger
        # Add a cancellation flag
        self._cancel_requested = threading.Event()

        # Create a publisher for velocity commands
        # self.cmd_vel_pub = self.navigator.create_publisher(
        #     Twist, '/cmd_vel', 10
        # )
        self._send_feedback = primitive._send_feedback

        # TF listener used to resolve local goals into LOCAL_GOAL_FIXED_FRAME
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self.navigator)

        # The exact goal this skill commands, resolved to its fixed frame
        # (map, or odom for local goals) -- lets UIs render the true target
        # rather than inferring it from the replanned path's endpoint.
        # Latched so a viewer that connects mid-navigation still sees it.
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._commanded_goal_pub = self.navigator.create_publisher(PoseStamped, "/nav/commanded_goal", latched)

        self.logger.info("Nav2 position primitive node created")

    def _lookup_fresh_base_pose(self, timeout_sec: float = 2.0, max_age_sec: float = 1.0):
        """Latest odom->base_link transform no older than max_age_sec, or None.

        Spins the navigator node so the TF listener actually receives data —
        outside Nav2's own blocking helpers nothing services its subscriptions.
        """
        navigator = self.navigator
        clock = navigator.get_clock()
        deadline = clock.now() + Duration(seconds=timeout_sec)
        min_stamp = clock.now() - Duration(seconds=max_age_sec)
        while clock.now() < deadline:
            rclpy.spin_once(navigator, timeout_sec=0.05)
            try:
                transform = self.tf_buffer.lookup_transform(LOCAL_GOAL_FIXED_FRAME, "base_link", Time())
            except TransformException:
                continue
            if Time.from_msg(transform.header.stamp) >= min_stamp:
                return transform
        return None

    def go_to_position(self, x: float, y: float, theta: float, local_frame: bool):
        """
        Sends a navigation goal to the navigator and waits until navigation ends.

        Args:
            x (float): x-coordinate of the target position.
            y (float): y-coordinate of the target position.
            theta (float): The orientation angle in radians.

        Returns:
            (TaskResult, str | None): the navigator's result status, plus a
            human-readable detail explaining a failure (None on success/cancel).
        """
        navigator = self.navigator
        # Reset cancellation flag
        self._cancel_requested.clear()

        # Determine behavior tree based on navigation mode
        behavior_tree = "mapfree" if local_frame else "navigation"

        # Local goals are resolved into the odom frame up front: Nav2 replans
        # the original goal at 1 Hz, and a base_link goal ages out of the
        # ~10 s TF buffer, breaking replans on longer navigations.
        if local_frame:
            base_tf = self._lookup_fresh_base_pose()
            if base_tf is None:
                self.logger.error(
                    f"No fresh base_link->{LOCAL_GOAL_FIXED_FRAME} transform available; cannot resolve local goal"
                )
                return TaskResult.FAILED, (
                    "could not determine the robot's current pose "
                    f"(no fresh {LOCAL_GOAL_FIXED_FRAME} transform), so the local goal could not be resolved"
                )
            base = base_tf.transform
            goal_x, goal_y, goal_yaw = resolve_local_goal(
                base.translation.x, base.translation.y, quaternion_to_yaw(base.rotation), x, y, theta
            )
            goal_frame = LOCAL_GOAL_FIXED_FRAME
            self.logger.info(
                f"Resolved local goal ({x}, {y}, {theta}) to {goal_frame} frame: "
                f"({goal_x:.3f}, {goal_y:.3f}, {goal_yaw:.3f})"
            )
        else:
            goal_x, goal_y, goal_yaw = x, y, theta
            goal_frame = "map"

        # Create a PoseStamped goal.
        goal_pose = PoseStamped()
        goal_pose.header.frame_id = goal_frame
        goal_pose.header.stamp = navigator.get_clock().now().to_msg()
        goal_pose.pose.position.x = goal_x
        goal_pose.pose.position.y = goal_y
        goal_pose.pose.position.z = 0.0

        goal_pose.pose.orientation.x = 0.0
        goal_pose.pose.orientation.y = 0.0
        goal_pose.pose.orientation.z = math.sin(goal_yaw / 2.0)
        goal_pose.pose.orientation.w = math.cos(goal_yaw / 2.0)

        self._commanded_goal_pub.publish(goal_pose)

        self.logger.debug(f"Sending goal pose ... behavior_tree: {behavior_tree}")
        path_navigator = self.navigator_mapfree if local_frame else self.navigator_navigation
        path = path_navigator.getPath(goal_pose, goal_pose, use_start=False)

        # If the path is None, we can't navigate to the goal
        if path is None:
            self.logger.error("Failed to get path to goal")
            return TaskResult.FAILED, (
                f"the planner found no path to ({goal_x:.2f}, {goal_y:.2f}) in the {goal_frame} frame "
                "— the goal may be unreachable, blocked, or outside the map"
            )

        navigator.goToPose(goal_pose, behavior_tree=behavior_tree)

        self.logger.debug("Waiting for navigation to complete ...")

        was_canceled = False
        initial_distance_remaining = -1.0  # Not set until the first valid feedback
        feedback_sent_close_to_goal = False  # Flag to track if feedback has been sent
        last_progress_log = 0.0
        # Last feedback snapshot, so a failure can say how far the robot got.
        last_distance_remaining = -1.0
        last_recoveries = 0

        # Modified loop to check for cancellation
        while not navigator.isTaskComplete():
            # Check if cancellation was requested
            if self._cancel_requested.is_set():
                self.logger.info("Cancellation detected in navigation loop")
                was_canceled = True
                navigator.cancelTask()
                break

            # Small sleep to prevent CPU hogging; also paces the 10 Hz cancel poll
            time.sleep(0.1)

            feedback = navigator.getFeedback()
            if not feedback:
                continue

            # Use Nav2's own distance_remaining: feedback.current_pose is
            # in the navigator's global frame while our goal may be in
            # another, so computing distances ourselves would be wrong.
            distance_remaining = feedback.distance_remaining
            last_recoveries = feedback.number_of_recoveries
            if distance_remaining > 0.0:
                last_distance_remaining = distance_remaining

            if initial_distance_remaining < 0.0 and distance_remaining > 0.0:
                initial_distance_remaining = distance_remaining

            path_completion = 0.0
            if initial_distance_remaining > 0.0:
                path_completion = max(0.0, min(100.0, (1.0 - distance_remaining / initial_distance_remaining) * 100.0))

            now = time.monotonic()
            if now - last_progress_log >= 1.0:
                last_progress_log = now
                self.logger.info(
                    f"Navigation progress: {path_completion:.0f}% ({distance_remaining:.2f}m remaining, "
                    f"{feedback.number_of_recoveries} recoveries)"
                )

            if 0.0 < distance_remaining < 0.2 and not feedback_sent_close_to_goal:
                self._send_feedback(
                    "I'm almost done with this movement, if I think I should navigate again to pursue this task"
                    ", I should stop the current primitive and start a new navigation movement."
                )
                feedback_sent_close_to_goal = True

        result = navigator.getResult()
        detail = None

        if was_canceled:
            self.logger.debug("Goal was canceled!")
            # This should not be necessary but somehow the navigator.cancelTask does not make result == TaskResult.CANCELED
            result = TaskResult.CANCELED
        elif result == TaskResult.SUCCEEDED:
            self.logger.debug("Goal succeeded!")
        else:
            self.logger.debug(f"Goal failed or timed out. result: {result}")
            detail = f"Nav2 reported {getattr(result, 'name', result)}"
            if last_distance_remaining >= 0.0:
                detail += f" with {last_distance_remaining:.2f}m still to go"
            if last_recoveries > 0:
                detail += f" after {last_recoveries} recovery attempt{'s' if last_recoveries != 1 else ''}"
            detail += " — the route may be blocked or the robot may be stuck"

        # Stop the robot by publishing a stop command.
        stop_cmd = Twist()
        stop_cmd.linear.x = 0.0
        stop_cmd.angular.z = 0.0
        # self.cmd_vel_pub.publish(stop_cmd)

        return result, detail

    def cancel_navigation(self):
        """
        Cancels the current navigation task.
        """
        self.logger.debug("Canceling current navigation task...")
        # Set the cancellation flag
        self._cancel_requested.set()

    def destroy(self):
        """Destroy the navigator nodes so their graph entities disappear now,
        not at some eventual GC pass. Safe outside go_to_position — nothing
        else spins these nodes."""
        for navigator in (self.navigator, self.navigator_mapfree, self.navigator_navigation):
            try:
                # Humble's BasicNavigator.destroy_node() misses this client; its
                # live handle would keep the rcl node and graph entities alive.
                navigator.assisted_teleop_client.destroy()
            except Exception as e:
                self.logger.warning(f"Error destroying assisted_teleop client: {e}")
            try:
                navigator.destroy_node()
            except Exception as e:
                self.logger.warning(f"Error destroying navigator node: {e}")


class NavigateToPosition(Skill):
    def __init__(self, logger):
        self.nav2_controller = Nav2Controller(logger, self)
        self.logger = logger

    @property
    def name(self):
        return "navigate_to_position"

    def guidelines(self):
        return (
            "Use when you need to navigate the robot to the specified position "
            "using provided x, y coordinates (meters), and theta_degrees (yaw) IN DEGREES. "
            "If local_frame is set to false, it navigates to a specific point in the map."
            "If local_frame is set to true, it navigates locally, where the robot is currently (0,0)"
        )

    def execute(self, x: float, y: float, theta_degrees: float = 0.0, local_frame: bool = False, **legacy):
        # The tool schema speaks theta_degrees, but the cloud agent and the
        # pose-adjustment pipeline speak `theta` in radians (the same keys the
        # robot's pose payload uses). The alias lives in **legacy so schema
        # introspection doesn't surface it as a second UI field.
        if legacy.get("theta") is not None:
            theta = float(legacy["theta"])
            theta_degrees = math.degrees(theta)
        else:
            theta = math.radians(theta_degrees)
        self.logger.info(
            f"Initiating navigation to position: x={x}, y={y}, theta_degrees={theta_degrees}, local_frame={local_frame}"
        )

        result, detail = self.nav2_controller.go_to_position(x, y, theta, local_frame)

        # Check if the navigation was canceled
        if result == TaskResult.CANCELED:
            self.logger.info("Navigation was canceled")
            return "Navigation canceled", SkillResult.CANCELLED
        elif result == TaskResult.SUCCEEDED:
            self.logger.info(
                f"Navigation complete. Arrived at position: x={x}, y={y}, theta_degrees={theta_degrees}, local_frame={local_frame}"
            )
            return f"Reached position ({x}, {y}, {theta_degrees} deg)", SkillResult.SUCCESS
        else:
            goal_desc = f"({x}, {y}, {theta_degrees} deg, {'local' if local_frame else 'map'} frame)"
            message = f"Navigation to {goal_desc} failed: {detail}"
            self.logger.info(message)
            return message, SkillResult.FAILURE

    def cancel(self):
        """
        Cancels the current navigation task.
        """
        self.logger.debug("Canceling navigation task")
        self.nav2_controller.cancel_navigation()
        return "Navigation canceled"

    def shutdown(self):
        """Destroy this instance's navigator nodes when the server retires it."""
        self.nav2_controller.destroy()
