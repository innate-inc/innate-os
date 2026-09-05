#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
NavigateToPoseRouter

This ROS 2 node acts as a router/proxy for the NavigateToPose action.
It listens on /navigate_to_pose and forwards all requests to /internal_navigate_to_pose.
This allows for interception, logging, or future middleware functionality.
"""

import threading

from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String

from mars_nav.navigation_policy import resolve_navigation_route


class NavigateToPoseRouter(Node):
    def __init__(self):
        super().__init__("navigate_to_pose_router")

        # Use separate mutually exclusive callback groups so server can call client
        self._server_callback_group = MutuallyExclusiveCallbackGroup()
        self._client_callback_group = MutuallyExclusiveCallbackGroup()

        # Track active goals for cancel forwarding
        self._goal_handle_map = {}  # Maps server goal_id (bytes) to client goal handle

        # Track current navigation mode
        # Fail closed until mode_manager's transient-local status arrives.
        self._current_mode = None
        # Once the co-located ModeManager supplies a synchronous update, it is
        # the authoritative mode source.  Older DDS samples may already be
        # queued and must never overwrite that update (especially `switching`,
        # which closes admission before lifecycle teardown).
        self._has_authoritative_mode_source = False
        # Goal admission and mode closure must be one atomic decision.  An
        # accepted goal holds a reservation until its outer execute callback
        # reaches a terminal path, including the internal handoff window.
        self._admission_lock = threading.Lock()
        self._pending_goal_reservations = 0

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
        """Track DDS mode status until the in-process manager takes over."""
        self.set_current_mode(msg.data, authoritative=False)

    def set_current_mode(self, mode, *, authoritative=True):
        """Synchronously update admission state from the in-process manager.

        The DDS subscription still serves late/cross-process updates, but mode
        teardown must not wait for asynchronous topic delivery to close goal
        admission.
        """
        next_mode = mode.strip().lower()
        with self._admission_lock:
            if not authoritative and self._has_authoritative_mode_source:
                self.get_logger().debug(f"Ignoring DDS mode update {next_mode!r}; in-process source is authoritative")
                return
            if authoritative:
                self._has_authoritative_mode_source = True
            if next_mode == self._current_mode:
                return
            self._current_mode = next_mode
            self.get_logger().debug(f"Current mode updated: {self._current_mode}")
            route = resolve_navigation_route(self._current_mode)
            if route is not None:
                self._publish_route(route)

    def get_pending_goal_count(self):
        """Return accepted outer goals that have not reached a terminal path."""
        with self._admission_lock:
            return self._pending_goal_reservations

    def _release_goal_reservation(self):
        with self._admission_lock:
            if self._pending_goal_reservations == 0:
                self.get_logger().error("Router goal reservation underflow")
                return
            self._pending_goal_reservations -= 1

    def _publish_route(self, route):
        planner_msg = String()
        planner_msg.data = route.planner
        self._current_planner_pub.publish(planner_msg)

        goal_checker_msg = String()
        goal_checker_msg.data = route.goal_checker
        self._current_goal_checker_pub.publish(goal_checker_msg)
        self.get_logger().info(f"Published planner: {route.planner}, goal_checker: {route.goal_checker}")

    def _goal_callback(self, goal_request):
        """Accept or reject incoming goal requests."""
        self.get_logger().info("Received navigate_to_pose goal request")
        with self._admission_lock:
            admitted_mode = self._current_mode
            route = resolve_navigation_route(admitted_mode, goal_request.behavior_tree)
            if route is not None:
                self._pending_goal_reservations += 1
        if route is None:
            requested = goal_request.behavior_tree or "<current mode>"
            self.get_logger().warn(
                f"Navigation goal rejected: selector {requested!r} is unavailable in mode {admitted_mode!r}"
            )
            return GoalResponse.REJECT
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

    async def _cancel_internal_and_wait_for_terminal(self, client_goal_handle):
        """Cancel a handed-off goal and wait until it can no longer drive.

        Register the result request before sending the cancel so lifecycle
        teardown cannot make us miss a fast terminal transition.  Humble's
        action futures do not expose a timeout argument, so this deliberately
        treats the terminal result as a safety barrier instead of returning
        while the internal goal may still be active.
        """
        result_future = client_goal_handle.get_result_async()
        cancel_accepted = False

        try:
            cancel_response = await client_goal_handle.cancel_goal_async()
            cancel_accepted = bool(cancel_response.goals_canceling)
            if cancel_accepted:
                self.get_logger().info("Internal action accepted the handoff cancel request")
            else:
                self.get_logger().error("Internal action rejected the handoff cancel request")
        except Exception as e:
            self.get_logger().error(f"Handoff cancel request failed: {e}")

        try:
            result_response = await result_future
        except Exception as e:
            self.get_logger().error(f"Failed waiting for the internal goal to terminate after handoff cancel: {e}")
            return cancel_accepted, None

        if result_response.status == 5:  # CANCELED
            self.get_logger().info("Internal goal reached CANCELED after handoff cancel")
        else:
            self.get_logger().error(
                f"Internal goal reached terminal status {result_response.status} instead of CANCELED "
                "after handoff cancel"
            )
        return cancel_accepted, result_response

    async def _execute_callback(self, goal_handle):
        """Execute one accepted goal while holding its admission reservation."""
        try:
            return await self._execute_reserved_goal(goal_handle)
        finally:
            self._release_goal_reservation()

    async def _execute_reserved_goal(self, goal_handle):
        """Execute callback that forwards the goal to internal action."""
        self.get_logger().info("Executing navigate_to_pose goal...")

        # Re-check after goal acceptance: a mode switch can race the action
        # handoff, and invalid modes must fail before waiting on a server that is
        # intentionally down.
        admitted_mode = self._current_mode
        admitted_route = resolve_navigation_route(admitted_mode, goal_handle.request.behavior_tree)
        if admitted_route is None:
            requested = goal_handle.request.behavior_tree or "<current mode>"
            self.get_logger().warn(
                f"Navigation rejected: selector {requested!r} is unavailable in mode {self._current_mode!r}"
            )
            goal_handle.abort()
            return NavigateToPose.Result()

        # Wait for the internal action server to be available
        if not self._action_client.wait_for_server(timeout_sec=5.0):
            self.get_logger().error("Internal navigate_to_pose action server not available!")
            goal_handle.abort()
            result = NavigateToPose.Result()
            return result

        # Create the goal to forward
        goal_msg = NavigateToPose.Goal()
        goal_msg.pose = goal_handle.request.pose
        # Leave behavior_tree empty, publish frame to /nav/current_frame instead

        # Resolve the selector against the lifecycle mode. Unknown values and
        # modes whose planner is inactive reject rather than silently falling
        # through to mapfree.
        # The lifecycle mode may have changed while wait_for_server blocked.
        admitted_mode = self._current_mode
        admitted_route = resolve_navigation_route(admitted_mode, goal_handle.request.behavior_tree)
        if admitted_route is None:
            requested = goal_handle.request.behavior_tree or "<current mode>"
            self.get_logger().warn(
                f"Navigation rejected: selector {requested!r} is unavailable in mode {self._current_mode!r}"
            )
            goal_handle.abort()
            result = NavigateToPose.Result()
            return result
        self._publish_route(admitted_route)

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

        # A mode switch may have closed admission while the internal action
        # request was in flight. Cancel that just-accepted goal immediately;
        # otherwise it can start driving after mode_manager's cancel sweep and
        # be stranded when the old lifecycle stack is torn down.
        current_route = resolve_navigation_route(self._current_mode, goal_handle.request.behavior_tree)
        if self._current_mode != admitted_mode or current_route != admitted_route:
            self.get_logger().warn(
                f"Navigation mode changed during goal handoff ({admitted_mode!r} -> {self._current_mode!r}); "
                "canceling the internal goal"
            )
            cancel_accepted, result_response = await self._cancel_internal_and_wait_for_terminal(client_goal_handle)
            internal_canceled = result_response is not None and result_response.status == 5
            if not cancel_accepted:
                self.get_logger().error("Handoff cancel was not acknowledged by the internal action server")
            if not internal_canceled:
                self.get_logger().error("Handoff failed to confirm that the internal goal was canceled")

            if internal_canceled and goal_handle.is_cancel_requested:
                goal_handle.canceled()
            else:
                goal_handle.abort()
            return result_response.result if result_response is not None else NavigateToPose.Result()

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
