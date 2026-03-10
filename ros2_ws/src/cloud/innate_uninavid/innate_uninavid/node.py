#!/usr/bin/env python3
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

import math
import os
import statistics
import time
from typing import Optional

import cv2
import numpy as np
import rclpy
from auth_client import AuthProvider
from dotenv import find_dotenv, load_dotenv
from geometry_msgs.msg import PoseStamped, Twist
from nav_msgs.msg import Path
from std_msgs.msg import Int32MultiArray
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage, Image

from innate_cloud_msgs.action import NavigateInstruction

from .ws_client import Action, ClientState, UninavidWsClient

def _yaw_to_quaternion(yaw: float) -> tuple[float, float, float, float]:
    """Convert a yaw angle to a quaternion (x, y, z, w)."""
    return (0.0, 0.0, math.sin(yaw / 2.0), math.cos(yaw / 2.0))


DEFAULT_WS_URL = "wss://office.servers.innatebotstatus.com:9000"
DEFAULT_AUTH_ISSUER_URL = "https://auth-v1.innate.bot"

_CMD_VEL: dict[int, tuple[float, float]] = {
    Action.STOP:    (0.0,  0.0),
    Action.FORWARD: (0.3,  0.0),
    Action.LEFT:    (0.0,  0.8),
    Action.RIGHT:   (0.0, -0.8),
}

_STOP = Twist()


def _twist(action: int) -> Optional[Twist]:
    if action not in _CMD_VEL:
        return None
    t = Twist()
    t.linear.x, t.angular.z = _CMD_VEL[action]
    return t


class UninavidNode(Node):

    def __init__(self) -> None:
        super().__init__("uninavid_node")

        env_path = find_dotenv(usecwd=True)
        if env_path:
            load_dotenv(env_path)

        self.declare_parameter("ws_url", os.getenv("UNINAVID_WS_URL", DEFAULT_WS_URL))
        self.declare_parameter("service_key", os.getenv("INNATE_SERVICE_KEY", ""))
        self.declare_parameter("auth_issuer_url", os.getenv("INNATE_AUTH_URL", DEFAULT_AUTH_ISSUER_URL))
        self.declare_parameter("cmd_duration_sec", 0.1)
        self.declare_parameter("cmd_publish_hz", 50.0)
        self.declare_parameter("poll_period_sec", 0.02)
        self.declare_parameter("latency_report_sec", 5.0)
        self.declare_parameter("image_send_hz", 49.0)
        self.declare_parameter("consecutive_stops_to_complete", 20)

        self._ws_url = str(self.get_parameter("ws_url").value)
        service_key = str(self.get_parameter("service_key").value)
        auth_issuer = str(self.get_parameter("auth_issuer_url").value)

        self._auth: Optional[AuthProvider] = (
            AuthProvider(issuer_url=auth_issuer, service_key=service_key)
            if service_key else None
        )

        self._client: Optional[UninavidWsClient] = None
        self._goal_handle = None
        self._last_rtt_report: float = 0.0

        self._image_sub = self.create_subscription(
            CompressedImage,
            "/mars/main_camera/left/image_raw/compressed",
            self._on_image,
            QoSProfile(reliability=ReliabilityPolicy.BEST_EFFORT,
                       history=HistoryPolicy.KEEP_LAST, depth=1),
            callback_group=MutuallyExclusiveCallbackGroup(),
        )
        self._cmd = self.create_publisher(Twist, "/cmd_vel", 10)
        self._plan_pub = self.create_publisher(Path, "/vln/plan", 10)
        self._actions_pub = self.create_publisher(Int32MultiArray, "/vln/actions", 10)
        self._debug_img_pub = self.create_publisher(Image, "/uninavid/debug_image", 1)

        # Dead-reckoning state for trajectory building
        self._traj_x: float = 0.0
        self._traj_y: float = 0.0
        self._traj_theta: float = 0.0
        self._traj_poses: list[tuple[float, float, float]] = []
        self._latest_jpeg: Optional[bytes] = None

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

    # ── Trajectory helpers ────────────────────────────────────────────────

    def _reset_trajectory(self) -> None:
        self._traj_x = 0.0
        self._traj_y = 0.0
        self._traj_theta = 0.0
        self._traj_poses = [(0.0, 0.0, 0.0)]
        self._latest_jpeg = None

    def _extend_trajectory(self, actions: list[int]) -> None:
        """Dead-reckon new actions into the running trajectory.

        Only appends a pose when position actually changes (FORWARD).
        Pure rotations (LEFT/RIGHT) update heading silently;
        STOP is ignored entirely.
        """
        dt = float(self.get_parameter("cmd_duration_sec").value)
        for code in actions:
            vel = _CMD_VEL.get(code)
            if vel is None:
                continue
            v, w = vel
            self._traj_theta += w * dt
            if v != 0.0:
                self._traj_x += v * math.cos(self._traj_theta) * dt
                self._traj_y += v * math.sin(self._traj_theta) * dt
                self._traj_poses.append(
                    (self._traj_x, self._traj_y, self._traj_theta)
                )

    def _publish_trajectory(self) -> None:
        """Publish accumulated trajectory as nav_msgs/Path on /plan."""
        path_msg = Path()
        path_msg.header.stamp = self.get_clock().now().to_msg()
        path_msg.header.frame_id = "odom"

        for x, y, theta in self._traj_poses:
            ps = PoseStamped()
            ps.header = path_msg.header
            ps.pose.position.x = x
            ps.pose.position.y = y
            ps.pose.position.z = 0.0
            q = _yaw_to_quaternion(theta)
            ps.pose.orientation.x = q[0]
            ps.pose.orientation.y = q[1]
            ps.pose.orientation.z = q[2]
            ps.pose.orientation.w = q[3]
            path_msg.poses.append(ps)

        self._plan_pub.publish(path_msg)

    def _publish_actions(self, actions: list[int]) -> None:
        """Publish the latest batch of action codes on /vln/actions for client-side overlay."""
        msg = Int32MultiArray()
        msg.data = actions
        self._actions_pub.publish(msg)

    def _publish_debug_image(self) -> None:
        """Overlay trajectory dots on the latest camera frame and publish."""
        jpeg = self._latest_jpeg
        if jpeg is None or len(self._traj_poses) < 2:
            return

        buf = np.frombuffer(jpeg, dtype=np.uint8)
        frame = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if frame is None:
            return

        h, w = frame.shape[:2]

        # Compute bounding box of trajectory for projection
        xs = [p[0] for p in self._traj_poses]
        ys = [p[1] for p in self._traj_poses]
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        # Add padding so dots aren't on the edge
        margin = 0.3
        x_range = max(x_max - x_min, 0.1) * (1 + margin)
        y_range = max(y_max - y_min, 0.1) * (1 + margin)
        x_center = (x_min + x_max) / 2.0
        y_center = (y_min + y_max) / 2.0

        # Fit trajectory into the bottom portion of the image
        img_margin = 20
        draw_w = w - 2 * img_margin
        draw_h = h // 3  # bottom third of the image
        draw_y0 = h - draw_h - img_margin

        # Semi-transparent overlay background
        overlay = frame.copy()
        cv2.rectangle(overlay, (img_margin - 5, draw_y0 - 5),
                      (img_margin + draw_w + 5, draw_y0 + draw_h + 5),
                      (0, 0, 0), cv2.FILLED)
        cv2.addWeighted(overlay, 0.4, frame, 0.6, 0, frame)

        def project(px: float, py: float) -> tuple[int, int]:
            u = int(img_margin + ((px - x_center) / x_range + 0.5) * draw_w)
            v = int(draw_y0 + (0.5 - (py - y_center) / y_range) * draw_h)
            return (u, v)

        # Draw trajectory line
        pts = [project(p[0], p[1]) for p in self._traj_poses]
        for i in range(1, len(pts)):
            cv2.line(frame, pts[i - 1], pts[i], (0, 255, 0), 2)

        # Draw dots at each waypoint
        r = max(3, int(0.008 * min(w, h)))
        for i, pt in enumerate(pts):
            color = (0, 0, 255) if i == len(pts) - 1 else (0, 200, 0)
            cv2.circle(frame, pt, r, color, cv2.FILLED)

        # Start marker
        cv2.circle(frame, pts[0], r + 2, (255, 255, 0), 2)

        # Overlay text
        n = len(self._traj_poses)
        cv2.putText(frame, f"traj pts: {n}", (img_margin, draw_y0 - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        # Publish as sensor_msgs/Image (bgr8)
        img_msg = Image()
        img_msg.header.stamp = self.get_clock().now().to_msg()
        img_msg.header.frame_id = "camera"
        img_msg.height = frame.shape[0]
        img_msg.width = frame.shape[1]
        img_msg.encoding = "bgr8"
        img_msg.is_bigendian = 0
        img_msg.step = frame.shape[1] * 3
        img_msg.data = frame.tobytes()
        self._debug_img_pub.publish(img_msg)

    # ── Image forwarding ──────────────────────────────────────────────────

    def _on_image(self, msg: CompressedImage) -> None:
        c = self._client
        if c is None or c.state != ClientState.CONNECTED:
            return
        s = msg.header.stamp
        now = self.get_clock().now().nanoseconds
        last = getattr(self, '_last_image_ns', 0)
        cam_dt = (now - last) / 1e9 if last > 0 else -1.0
        self._last_image_ns = now
        self.get_logger().info(
            f"push_frame stamp={s.sec}.{s.nanosec:09d} ({len(msg.data)} bytes) cam_dt={cam_dt:.3f}s"
        )
        c.push_frame(format=msg.format, stamp_sec=s.sec,
                     stamp_nanosec=s.nanosec, data=bytes(msg.data))
        # Stash latest JPEG for debug overlay
        self._latest_jpeg = bytes(msg.data)

    # ── Goal execution ────────────────────────────────────────────────────

    def _execute(self, goal_handle):
        instruction = goal_handle.request.instruction
        self.get_logger().info(f"Executing: {instruction!r}")

        cmd_duration = float(self.get_parameter("cmd_duration_sec").value)
        cmd_publish_hz = float(self.get_parameter("cmd_publish_hz").value)
        poll_period = float(self.get_parameter("poll_period_sec").value)
        latency_report = float(self.get_parameter("latency_report_sec").value)
        image_send_hz = float(self.get_parameter("image_send_hz").value)
        consecutive_stops = int(self.get_parameter("consecutive_stops_to_complete").value)

        client = UninavidWsClient(
            url=self._ws_url, auth_provider=self._auth,
            logger=self.get_logger(),
            image_send_hz=image_send_hz,
            consecutive_stops_to_complete=consecutive_stops,
        )
        self._client = client
        client.connect(instruction)
        self._last_rtt_report = 0.0
        self._reset_trajectory()

        result = NavigateInstruction.Result()
        feedback = NavigateInstruction.Feedback()

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
                code = client.pop_action()
                while code is not None:
                    if not goal_handle.is_active or goal_handle.is_cancel_requested:
                        break
                    if client.state in (ClientState.FAILED, ClientState.DISCONNECTED,
                                        ClientState.COMPLETED):
                        break

                    label = Action(code).name if code in Action._value2member_map_ else str(code)
                    self.get_logger().info(f"Executing action: {label}")

                    tw = _twist(code)
                    if tw is not None:
                        deadline = time.monotonic() + cmd_duration
                        dt = 1.0 / cmd_publish_hz
                        while time.monotonic() < deadline:
                            self._cmd.publish(tw)
                            time.sleep(dt)
                        self._cmd.publish(_STOP)

                    # Publish trajectory + actions incrementally after each action
                    new_actions = client.pop_action_history()
                    if new_actions:
                        self._publish_actions(new_actions)
                        self._extend_trajectory(new_actions)
                        self._publish_trajectory()
                        self._publish_debug_image()

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

                # Catch any remaining trajectory actions outside the drain loop
                new_actions = client.pop_action_history()
                if new_actions:
                    self._publish_actions(new_actions)
                    self._extend_trajectory(new_actions)
                    self._publish_trajectory()
                    self._publish_debug_image()

                # Feedback
                feedback.latest_action = code if code is not None else 0
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
