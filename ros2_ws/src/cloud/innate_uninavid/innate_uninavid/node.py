#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
ROS 2 node for Innate UniNavid.

- Subscribes to /mars/main_camera/left/image_raw/compressed
  and forwards each frame to the UniNavid cloud websocket.

- Exposes a ``navigate_instruction`` action server.  New goals
  preempt the previous one via handle_accepted_callback (Nav2 pattern).

Manual test::

    ros2 action send_goal /navigate_instruction \
        innate_cloud_msgs/action/NavigateInstruction \
        '{instruction: "go to the red chair and stop"}' --feedback
"""

from __future__ import annotations

import os
import statistics
import threading
import time

import rclpy
from auth_client import AuthProvider
from dotenv import find_dotenv, load_dotenv
from geometry_msgs.msg import Twist
from innate_cloud_msgs.action import NavigateInstruction
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import Int32MultiArray

from .ws_client import Action, ClientState, UninavidWsClient

DEFAULT_WS_URL = "wss://uninavid-v1.svc.innate.bot"
DEFAULT_AUTH_ISSUER_URL = "https://auth-v1.svc.innate.bot"

# Default drive speeds for UniNavid's discrete actions (overridable via uninavid_node params).
_DEFAULT_FORWARD_SPEED = 0.3  # m/s, FORWARD action
_DEFAULT_TURN_SPEED = 0.8  # rad/s, LEFT / RIGHT actions

_STOP = Twist()


def _twist(cmd_vel: dict[int, tuple[float, float]], action: int) -> Twist | None:
    if action not in cmd_vel:
        return None
    t = Twist()
    t.linear.x, t.angular.z = cmd_vel[action]
    return t


def _positive_or_default(value, default: float, name: str, logger) -> float:
    """Clamp a non-positive rate/period override to ``default``, warning when it does.

    These knobs feed ``1.0 / x`` divisions (cmd_publish_hz, image_send_hz) and
    ``time.sleep`` (poll_period_sec, cmd_duration_sec). A 0 or negative value — which
    config/settings.yaml's int-vs-double guard does not reject — would otherwise crash
    mid-goal with ZeroDivisionError or a negative ``time.sleep``. Mirrors the
    inference_hz guard in manipulation_server.py.
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 0.0
    if v > 0.0:
        return v
    logger.warn(f"{name} must be > 0 (got {value!r}); falling back to {default}.")
    return default


class UninavidNode(Node):
    def __init__(self) -> None:
        super().__init__("uninavid_node")

        env_path = find_dotenv(usecwd=True)
        if env_path:
            load_dotenv(env_path)

        self.declare_parameter("ws_url", os.getenv("UNINAVID_WS_URL", DEFAULT_WS_URL))
        self.declare_parameter("auth_issuer_url", os.getenv("INNATE_AUTH_URL", DEFAULT_AUTH_ISSUER_URL))
        self.declare_parameter("cmd_duration_sec", 0.1)
        self.declare_parameter("cmd_publish_hz", 50.0)
        self.declare_parameter("poll_period_sec", 0.02)
        self.declare_parameter("latency_report_sec", 5.0)
        self.declare_parameter("image_send_hz", 49.0)
        self.declare_parameter("consecutive_stops_to_complete", 20)
        # Drive speeds, tuned independently of teleop (motion_control) and nav2 (nav).
        self.declare_parameter("forward_speed", _DEFAULT_FORWARD_SPEED)
        self.declare_parameter("turn_speed", _DEFAULT_TURN_SPEED)
        forward_speed = float(self.get_parameter("forward_speed").value)
        turn_speed = float(self.get_parameter("turn_speed").value)
        self._cmd_vel: dict[int, tuple[float, float]] = {
            Action.STOP: (0.0, 0.0),
            Action.FORWARD: (forward_speed, 0.0),
            Action.LEFT: (0.0, turn_speed),
            Action.RIGHT: (0.0, -turn_speed),
        }

        self._ws_url = str(self.get_parameter("ws_url").value)
        # ROS parameters and /parameter_events are readable through rosbridge.
        # Credentials stay in process configuration, never in the ROS graph.
        service_key = os.getenv("INNATE_SERVICE_KEY", "")
        self._auth_issuer = str(self.get_parameter("auth_issuer_url").value)
        self._config_lock = threading.Lock()
        self._service_key = service_key.strip()

        self._auth: AuthProvider | None = self._make_auth_provider(self._service_key)
        if self._auth is None:
            self.get_logger().warn(
                "UniNavid service key is missing. "
                "Vision navigation goals will fail until INNATE_SERVICE_KEY is "
                "configured and the node is restarted."
            )

        self._client: UninavidWsClient | None = None
        self._goal_handle = None
        self._last_rtt_report: float = 0.0

        self._image_sub = self.create_subscription(
            CompressedImage,
            "/mars/main_camera/left/image_raw/compressed",
            self._on_image,
            QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST, depth=1),
            callback_group=MutuallyExclusiveCallbackGroup(),
        )
        self._cmd = self.create_publisher(Twist, "/cmd_vel", 10)
        self._actions_pub = self.create_publisher(Int32MultiArray, "/vln/actions", 10)

        self._action_server = ActionServer(
            self,
            NavigateInstruction,
            "navigate_instruction",
            execute_callback=self._execute,
            goal_callback=lambda _: GoalResponse.ACCEPT,
            handle_accepted_callback=self._on_accepted,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=ReentrantCallbackGroup(),
        )
        self.get_logger().info("UninavidNode ready")

    @staticmethod
    def _is_configured_service_key(service_key: str) -> bool:
        return bool((service_key or "").strip())

    def _make_auth_provider(self, service_key: str) -> AuthProvider | None:
        if not self._is_configured_service_key(service_key):
            return None
        return AuthProvider(issuer_url=self._auth_issuer, service_key=service_key)

    # ── Preemption (Nav2 pattern) ─────────────────────────────────────────

    def _on_accepted(self, goal_handle):
        old = self._goal_handle
        if old is not None and old.is_active:
            self.get_logger().info("Preempting previous goal")
            old.abort()
            if self._client is not None:
                self._client.disconnect()
        self._goal_handle = goal_handle
        goal_handle.execute()

    # ── Actions publisher ────────────────────────────────────────────────

    def _publish_actions(self, actions: list[int]) -> None:
        """Publish the latest batch of action codes on /vln/actions for client-side overlay."""
        msg = Int32MultiArray()
        msg.data = actions
        self._actions_pub.publish(msg)

    # ── Image forwarding ──────────────────────────────────────────────────

    def _on_image(self, msg: CompressedImage) -> None:
        c = self._client
        if c is None or c.state != ClientState.CONNECTED:
            return
        s = msg.header.stamp
        now = self.get_clock().now().nanoseconds
        last = getattr(self, "_last_image_ns", 0)
        cam_dt = (now - last) / 1e9 if last > 0 else -1.0
        self._last_image_ns = now
        self.get_logger().info(f"push_frame stamp={s.sec}.{s.nanosec:09d} ({len(msg.data)} bytes) cam_dt={cam_dt:.3f}s")
        c.push_frame(format=msg.format, stamp_sec=s.sec, stamp_nanosec=s.nanosec, data=bytes(msg.data))

    # ── Goal execution ────────────────────────────────────────────────────

    def _execute(self, goal_handle):
        instruction = goal_handle.request.instruction
        self.get_logger().info(f"Executing: {instruction!r}")

        result = NavigateInstruction.Result()
        feedback = NavigateInstruction.Feedback()

        with self._config_lock:
            auth = self._auth
            ws_url = self._ws_url

        if auth is None:
            self._cmd.publish(_STOP)
            goal_handle.abort()
            return self._result(
                result,
                False,
                "Missing INNATE_SERVICE_KEY for UniNavid.",
            )

        log = self.get_logger()
        cmd_duration = _positive_or_default(self.get_parameter("cmd_duration_sec").value, 0.1, "cmd_duration_sec", log)
        cmd_publish_hz = _positive_or_default(self.get_parameter("cmd_publish_hz").value, 50.0, "cmd_publish_hz", log)
        poll_period = _positive_or_default(self.get_parameter("poll_period_sec").value, 0.02, "poll_period_sec", log)
        latency_report = float(self.get_parameter("latency_report_sec").value)
        # Must stay positive: UninavidWsClient divides 1.0 / hz.
        image_send_hz = _positive_or_default(self.get_parameter("image_send_hz").value, 49.0, "image_send_hz", log)
        consecutive_stops = int(self.get_parameter("consecutive_stops_to_complete").value)

        client = UninavidWsClient(
            url=ws_url,
            auth_provider=auth,
            logger=self.get_logger(),
            image_send_hz=image_send_hz,
            consecutive_stops_to_complete=consecutive_stops,
        )
        self._client = client
        client.connect(instruction)
        self._last_rtt_report = 0.0

        try:
            while rclpy.ok() and goal_handle.is_active:
                if goal_handle.is_cancel_requested:
                    self._cmd.publish(_STOP)
                    goal_handle.canceled()
                    return self._result(result, False, "Canceled")

                state = client.state

                if state == ClientState.FAILED:
                    self._cmd.publish(_STOP)
                    goal_handle.abort()
                    return self._result(result, False, client.error_message or "WS failed")

                if state == ClientState.DISCONNECTED:
                    self._cmd.publish(_STOP)
                    goal_handle.abort()
                    return self._result(result, False, "WS disconnected")

                if state == ClientState.COMPLETED:
                    self._cmd.publish(_STOP)
                    goal_handle.succeed()
                    return self._result(result, True, "Navigation completed")

                # Drain & execute actions
                now = time.monotonic()
                executed_action = None
                code = client.pop_action()
                while code is not None:
                    if not goal_handle.is_active or goal_handle.is_cancel_requested:
                        break
                    if client.state in (ClientState.FAILED, ClientState.DISCONNECTED, ClientState.COMPLETED):
                        break

                    executed_action = code
                    label = Action(code).name if code in Action._value2member_map_ else str(code)
                    self.get_logger().info(f"Executing action: {label}")

                    tw = _twist(self._cmd_vel, code)
                    if tw is not None:
                        deadline = time.monotonic() + cmd_duration
                        dt = 1.0 / cmd_publish_hz
                        while time.monotonic() < deadline:
                            self._cmd.publish(tw)
                            time.sleep(dt)
                        self._cmd.publish(_STOP)

                    new_actions = client.pop_action_history()
                    if new_actions:
                        self._publish_actions(new_actions)

                    code = client.pop_action()

                # RTT
                if now - self._last_rtt_report >= latency_report:
                    self._last_rtt_report = now
                    samples = client.pop_rtt_samples()
                    if samples:
                        self.get_logger().info(
                            f"RTT (n={len(samples)}): min={min(samples):.3f}s "
                            f"med={statistics.median(samples):.3f}s max={max(samples):.3f}s"
                        )

                # Feedback
                feedback.latest_action = executed_action if executed_action is not None else 0
                feedback.consecutive_stops = client.consecutive_stops
                feedback.max_consecutive_stops = consecutive_stops
                if goal_handle.is_active:
                    goal_handle.publish_feedback(feedback)

                time.sleep(poll_period)

            # Preempted (is_active became False) or shutting down
            self._cmd.publish(_STOP)
            self.get_logger().info("Goal preempted or node stopping")
            return self._result(result, False, "Preempted")

        finally:
            client.disconnect()
            if self._client is client:
                self._client = None

    @staticmethod
    def _result(result, success, message):
        result.success = success
        result.message = message
        return result


def main(args=None) -> None:
    rclpy.init(args=args)
    node = UninavidNode()
    executor = MultiThreadedExecutor(num_threads=2)
    try:
        rclpy.spin(node, executor=executor)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
