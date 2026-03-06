#!/usr/bin/env python3
"""
ROS 2 node for Innate UniNavid.

- Subscribes to ``/mars/main_camera/left/image_raw/compressed``
  and pushes frames to the websocket client.

- Exposes a ``navigate_instruction`` action server.  When a goal is
  accepted it hands the instruction to the websocket client and polls
  for action codes via a ROS timer.

- Publishes ``/cmd_vel`` based on integer action codes received from
  the websocket.

All websocket I/O lives in :pymod:`innate_uninavid.ws_client`.
This file contains **only** ROS code.

Manual test (CLI)::

    ros2 action send_goal /navigate_instruction \\
        innate_cloud_msgs/action/NavigateInstruction \\
        '{instruction: "go to the red chair and stop"}' \\
        --feedback
"""

from __future__ import annotations

import os
import threading
from typing import Optional

import rclpy
from auth_client import AuthProvider
from dotenv import find_dotenv, load_dotenv
from geometry_msgs.msg import Twist
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage

from innate_cloud_msgs.action import NavigateInstruction

from innate_uninavid.ws_client import (
    Action,
    ClientState,
    UninavidWsClient,
)

# ── Config ────────────────────────────────────────────────────────────────────
CMD_DURATION_SEC: float = 0.15
CMD_PUBLISH_HZ: float = 10.0
POLL_HZ: float = 50.0          # how often the timer checks the ws client

DEFAULT_WS_URL = "wss://office.servers.innatebotstatus.com:9000"
DEFAULT_AUTH_ISSUER_URL = "https://auth-v1.innate.bot"

# Velocity lookup: action code -> (linear.x, angular.z)
_CMD_VEL: dict[int, tuple[float, float]] = {
    Action.STOP:    (0.0,  0.0),
    Action.FORWARD: (0.3,  0.0),
    Action.LEFT:    (0.0,  0.8),
    Action.RIGHT:   (0.0, -0.8),
}


class UninavidNode(Node):
    """Pure-ROS node that delegates all websocket I/O to UninavidWsClient."""

    def __init__(self) -> None:
        super().__init__("uninavid_node")
        self.get_logger().info("UninavidNode starting")

        # ── .env ──────────────────────────────────────────────────────────────
        env_path = find_dotenv(usecwd=True)
        if env_path:
            load_dotenv(env_path)
            self.get_logger().info(f"Loaded .env from {env_path}")
        else:
            self.get_logger().warning("No .env file found")

        # ── Parameters ────────────────────────────────────────────────────────
        self.declare_parameter("ws_url", os.getenv("UNINAVID_WS_URL", DEFAULT_WS_URL))
        self.declare_parameter("service_key", os.getenv("INNATE_SERVICE_KEY", ""))
        self.declare_parameter("auth_issuer_url", os.getenv("INNATE_AUTH_URL", DEFAULT_AUTH_ISSUER_URL))

        ws_url = str(self.get_parameter("ws_url").value)
        service_key = str(self.get_parameter("service_key").value)
        auth_issuer = str(self.get_parameter("auth_issuer_url").value)
        self._ws_url = ws_url
        self.get_logger().info(f"Websocket target: {ws_url}")

        # ── Auth ──────────────────────────────────────────────────────────────
        if service_key:
            self._auth: Optional[AuthProvider] = AuthProvider(
                issuer_url=auth_issuer, service_key=service_key,
            )
            self.get_logger().info("Auth configured")
        else:
            self._auth = None
            self.get_logger().warning("No INNATE_SERVICE_KEY — no auth")

        # ── Websocket client (lives in its own thread) ────────────────────────
        self._ws: Optional[UninavidWsClient] = None

        # ── cmd_vel state ─────────────────────────────────────────────────────
        self._cmd_vel_pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self._hold_remaining: float = 0.0
        self._current_twist: Optional[Twist] = None

        # ── Image subscriber ──────────────────────────────────────────────────
        image_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )
        self._image_sub = self.create_subscription(
            CompressedImage,
            "/mars/main_camera/left/image_raw/compressed",
            self._on_image,
            image_qos,
        )

        # ── Action server ─────────────────────────────────────────────────────
        self._active_goal_handle = None
        self._active_goal_lock = threading.Lock()
        self._goal_abort = threading.Event()

        self._action_server = ActionServer(
            self,
            NavigateInstruction,
            "navigate_instruction",
            execute_callback=self._execute_goal,
            goal_callback=self._handle_goal,
            cancel_callback=self._handle_cancel,
            callback_group=ReentrantCallbackGroup(),
        )
        self.get_logger().info("Action server 'navigate_instruction' ready")

        # ── Timers ────────────────────────────────────────────────────────────
        poll_period = 1.0 / POLL_HZ
        cmd_period = 1.0 / CMD_PUBLISH_HZ
        self._poll_timer = self.create_timer(poll_period, self._poll_ws)
        self._cmd_timer = self.create_timer(cmd_period, self._publish_cmd_vel)

    # ── Image callback ────────────────────────────────────────────────────────

    def _on_image(self, msg: CompressedImage) -> None:
        ws = self._ws
        if ws is not None:
            ws.push_frame(
                format=msg.format,
                stamp_sec=msg.header.stamp.sec,
                stamp_nanosec=msg.header.stamp.nanosec,
                data=bytes(msg.data),
            )

    # ── Timer: poll websocket for new actions ─────────────────────────────────

    def _poll_ws(self) -> None:
        ws = self._ws
        if ws is None:
            return
        action = ws.pop_action()
        if action is None:
            return
        if action not in _CMD_VEL:
            return

        lin_x, ang_z = _CMD_VEL[action]
        twist = Twist()
        twist.linear.x = lin_x
        twist.angular.z = ang_z
        self._current_twist = twist
        self._hold_remaining = CMD_DURATION_SEC

    # ── Timer: publish cmd_vel while holding ──────────────────────────────────

    def _publish_cmd_vel(self) -> None:
        if self._current_twist is not None and self._hold_remaining > 0:
            self._cmd_vel_pub.publish(self._current_twist)
            self._hold_remaining -= 1.0 / CMD_PUBLISH_HZ
            if self._hold_remaining <= 0:
                self._cmd_vel_pub.publish(Twist())  # final stop
                self._current_twist = None

    # ── Action server callbacks ───────────────────────────────────────────────

    def _handle_goal(self, goal_request) -> GoalResponse:
        self.get_logger().info(f"Goal received: {goal_request.instruction!r}")
        with self._active_goal_lock:
            if self._active_goal_handle is not None:
                self.get_logger().info("Preempting previous goal")
                self._goal_abort.set()
        return GoalResponse.ACCEPT

    def _handle_cancel(self, goal_handle) -> CancelResponse:
        self.get_logger().info("Goal cancel requested")
        self._goal_abort.set()
        return CancelResponse.ACCEPT

    def _execute_goal(self, goal_handle):
        """Block until the ws client signals completed / failed / canceled."""
        instruction = goal_handle.request.instruction
        self.get_logger().info(f"Executing goal: {instruction!r}")

        self._goal_abort.clear()

        with self._active_goal_lock:
            self._active_goal_handle = goal_handle

        # Spin up a fresh websocket client
        ws = UninavidWsClient(
            url=self._ws_url,
            auth_provider=self._auth,
            logger=self.get_logger(),
        )
        self._ws = ws
        ws.connect(instruction)

        result = NavigateInstruction.Result()
        feedback = NavigateInstruction.Feedback()

        try:
            while rclpy.ok():
                # Cancel / preempt
                if self._goal_abort.is_set() or goal_handle.is_cancel_requested:
                    self.get_logger().info("Goal canceled / preempted")
                    ws.disconnect()
                    goal_handle.canceled()
                    result.success = False
                    result.message = "Canceled"
                    return result

                state = ws.state

                if state == ClientState.FAILED:
                    msg = ws.error_message or "Connection failed"
                    self.get_logger().error(f"WS failed: {msg}")
                    goal_handle.abort()
                    result.success = False
                    result.message = msg
                    return result

                if state == ClientState.COMPLETED:
                    self.get_logger().info("Goal succeeded (consecutive STOPs)")
                    goal_handle.succeed()
                    result.success = True
                    result.message = "Navigation completed"
                    return result

                if state == ClientState.DISCONNECTED:
                    reason = ws.error_message or "unknown reason"
                    self.get_logger().warning(f"WS disconnected: {reason}")
                    goal_handle.abort()
                    result.success = False
                    result.message = f"Websocket disconnected: {reason}"
                    return result

                # Publish feedback
                feedback.latest_action = 0
                feedback.consecutive_stops = ws.consecutive_stops
                try:
                    goal_handle.publish_feedback(feedback)
                except Exception:
                    self.get_logger().warning("Feedback failed — client gone")
                    ws.disconnect()
                    try:
                        goal_handle.abort()
                    except Exception:
                        pass
                    result.success = False
                    result.message = "Client disconnected"
                    return result

                # Wait a bit before next poll (also wakes on abort)
                self._goal_abort.wait(timeout=0.1)

            # Node shutting down
            ws.disconnect()
            goal_handle.abort()
            result.success = False
            result.message = "Node shutting down"
            return result

        finally:
            ws.disconnect()
            self._ws = None
            self._current_twist = None
            self._cmd_vel_pub.publish(Twist())  # safety stop
            with self._active_goal_lock:
                if self._active_goal_handle is goal_handle:
                    self._active_goal_handle = None


def main(args=None) -> None:
    rclpy.init(args=args)
    node = UninavidNode()
    executor = MultiThreadedExecutor()
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
