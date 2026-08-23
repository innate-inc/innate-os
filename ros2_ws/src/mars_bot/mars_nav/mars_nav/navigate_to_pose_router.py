#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
NavigateToPoseRouter

This ROS 2 node acts as a router/proxy for the NavigateToPose action.
It listens on /navigate_to_pose and forwards all requests to /internal_navigate_to_pose.
This allows for interception, logging, or future middleware functionality.
"""

import os

from ament_index_python.packages import get_package_share_directory
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String


class NavigateToPoseRouter(Node):
    def __init__(self):
        super().__init__("navigate_to_pose_router")

        # Use separate mutually exclusive callback groups so server can call client
        self._server_callback_group = MutuallyExclusiveCallbackGroup()
        self._client_callback_group = MutuallyExclusiveCallbackGroup()

        # Track active goals for cancel forwarding
        self._goal_handle_map = {}  # Maps server goal_id (bytes) to client goal handle

        # Track current navigation mode
        self._current_mode = "mapfree"  # Default mode
        self._person_follow_tree = os.path.join(
            get_package_share_directory("mars_nav"),
            "config",
            "nav_to_person.xml",
        )

        # QoS profile for persistent/latched topic
        latched_qos = QoSProfile(
            depth=1, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL, reliability=QoSReliabilityPolicy.RELIABLE
        )

        # Subscribe to current navigation mode
        self._mode_sub = self.create_subscription(String, "/nav/current_mode", self._mode_callback, latched_qos)

        # Publisher for current planner selection
        self._current_planner_pub = self.create_publisher(String, "/nav/current_planner", latched_qos)

        # Publisher for current goal checker selection
        self._current_goal_checker_pub = self.create_publisher(String, "/nav/current_goal_checker", latched_qos)

        # Publish initial values at startup (mapfree mode)
        initial_planner = String()
        initial_planner.data = "mapfree"
        self._current_planner_pub.publish(initial_planner)

        initial_goal_checker = String()
        initial_goal_checker.data = "goal_checker_precise"
        self._current_goal_checker_pub.publish(initial_goal_checker)

        self.get_logger().info("Published initial planner: mapfree, goal_checker: goal_checker_precise")

        # Create action client to forward requests to internal action
        self._action_client = ActionClient(
            self, NavigateToPose, "/internal_navigate_to_pose", callback_group=self._client_callback_group
        )

        # Create action server to receive requests
        self._action_server = ActionServer(
            self,
            NavigateToPose,
            "/navigate_to_pose",
            execute_callback=self._execute_callback,
            goal_callback=self._goal_callback,
            cancel_callback=self._cancel_callback,
            callback_group=self._server_callback_group,
        )

        self.get_logger().info("NavigateToPoseRouter initialized")
        self.get_logger().info("  Listening on: /navigate_to_pose")
        self.get_logger().info("  Forwarding to: /internal_navigate_to_pose")

    def _mode_callback(self, msg):
        """Track the current navigation mode."""
        self._current_mode = msg.data
        self.get_logger().debug(f"Current mode updated: {self._current_mode}")

    def _goal_callback(self, goal_request):
        """Accept or reject incoming goal requests."""
        self.get_logger().info("Received navigate_to_pose goal request")
        return GoalResponse.ACCEPT

    def _cancel_callback(self, goal_handle):
        """Handle cancel requests by forwarding to internal action.

        If the internal goal hasn't been accepted yet (its handle isn't in the
        map), the cancel is NOT lost: accepting here flips the server goal to
        CANCELING, and _execute_callback re-checks is_cancel_requested right
        after internal acceptance and forwards the cancel then.
        """
        self.get_logger().info("Received cancel request")

        # Try to cancel the corresponding internal goal
        goal_id = bytes(goal_handle.goal_id.uuid)
        if goal_id in self._goal_handle_map:
            client_goal_handle = self._goal_handle_map[goal_id]
            if client_goal_handle is not None:
                self.get_logger().info("Forwarding cancel to internal action")
                self._forward_cancel(client_goal_handle)

        return CancelResponse.ACCEPT

    def _forward_cancel(self, client_goal_handle):
        """Cancel the internal goal and log the outcome (a rejected cancel
        would otherwise be invisible and the robot would keep driving)."""

        def _on_cancel_response(future):
            try:
                response = future.result()
                if not response.goals_canceling:
                    self.get_logger().warn("Internal action rejected the cancel request")
            except Exception as e:
                self.get_logger().error(f"Cancel forwarding failed: {e}")

        client_goal_handle.cancel_goal_async().add_done_callback(_on_cancel_response)

    async def _execute_callback(self, goal_handle):
        """Execute callback that forwards the goal to internal action."""
        self.get_logger().info("Executing navigate_to_pose goal...")

        # Wait for the internal action server to be available
        if not self._action_client.wait_for_server(timeout_sec=5.0):
            self.get_logger().error("Internal navigate_to_pose action server not available!")
            goal_handle.abort()
            result = NavigateToPose.Result()
            return result

        # Create the goal to forward
        goal_msg = NavigateToPose.Goal()
        goal_msg.pose = goal_handle.request.pose

        route = goal_handle.request.behavior_tree or self._current_mode

        if route == "mapping" or (route == "person_follow" and self._current_mode == "mapping"):
            self.get_logger().warn("Navigation rejected: currently in mapping mode")
            goal_handle.abort()
            result = NavigateToPose.Result()
            return result

        if route == "person_follow":
            planner = "mapfree"
            goal_checker = "goal_checker_precise"
            goal_msg.behavior_tree = self._person_follow_tree
        elif route == "navigation":
            planner = "navigation"
            goal_checker = "goal_checker"
        else:  # mapfree or any other
            planner = "mapfree"
            goal_checker = "goal_checker_precise"

        # Publish the planner selection
        planner_msg = String()
        planner_msg.data = planner
        self._current_planner_pub.publish(planner_msg)

        # Publish the goal checker selection
        goal_checker_msg = String()
        goal_checker_msg.data = goal_checker
        self._current_goal_checker_pub.publish(goal_checker_msg)

        self.get_logger().info(f"Published planner: {planner}, goal_checker: {goal_checker}")

        self.get_logger().info(
            f"Forwarding goal to /nav/navigate_to_pose: "
            f"position=({goal_msg.pose.pose.position.x:.2f}, "
            f"{goal_msg.pose.pose.position.y:.2f}, "
            f"{goal_msg.pose.pose.position.z:.2f})"
        )

        # Send goal to internal action server
        send_goal_future = self._action_client.send_goal_async(
            goal_msg, feedback_callback=lambda feedback: self._feedback_callback(goal_handle, feedback)
        )

        # Wait for goal acceptance
        client_goal_handle = await send_goal_future

        if not client_goal_handle.accepted:
            self.get_logger().warn("Internal goal was rejected")
            goal_handle.abort()
            result = NavigateToPose.Result()
            return result

        self.get_logger().info("Internal goal accepted")

        # Store mapping for cancel handling
        goal_id = bytes(goal_handle.goal_id.uuid)
        self._goal_handle_map[goal_id] = client_goal_handle

        # A cancel may have arrived while we were waiting for internal
        # acceptance (before the map entry above existed). _cancel_callback
        # already ACCEPTed it — without this check the cancel would be dropped
        # and the robot would drive to a goal the caller believes is canceled.
        if goal_handle.is_cancel_requested:
            self.get_logger().info("Cancel arrived during goal handoff; forwarding to internal action")
            self._forward_cancel(client_goal_handle)

        # Wait for the result
        try:
            result_future = client_goal_handle.get_result_async()
            result_response = await result_future

            # Forward the result status
            if result_response.status == 4:  # SUCCEEDED
                self.get_logger().info("Internal goal succeeded")
                goal_handle.succeed()
            elif result_response.status == 5:  # CANCELED
                self.get_logger().info("Internal goal was canceled")
                goal_handle.canceled()
            else:  # ABORTED or other failure
                self.get_logger().warn(f"Internal goal failed with status: {result_response.status}")
                goal_handle.abort()

            return result_response.result

        except Exception as e:
            self.get_logger().error(f"Error waiting for internal result: {e}")
            goal_handle.abort()
            result = NavigateToPose.Result()
            return result
        finally:
            # Clean up goal handle mapping
            goal_id = bytes(goal_handle.goal_id.uuid)
            if goal_id in self._goal_handle_map:
                del self._goal_handle_map[goal_id]

    def _feedback_callback(self, server_goal_handle, feedback_msg):
        """Forward feedback from internal action to the original client."""
        # feedback_msg is a NavigateToPose.Impl.FeedbackMessage
        self.get_logger().debug("Forwarding feedback")
        server_goal_handle.publish_feedback(feedback_msg.feedback)
