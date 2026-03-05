#!/usr/bin/env python3
"""
ROS 2 node for Innate UniNavid.

- Subscribes to /mars/main_camera/left/image_raw/compressed
  (sensor_msgs/CompressedImage) and forwards each frame to a cloud
  websocket at a configurable rate.

- Exposes a ``navigate_instruction`` action server
  (innate_cloud_msgs/action/NavigateInstruction).  When a goal is
  accepted the instruction is forwarded over the websocket; action
  codes coming back drive /cmd_vel.  The goal succeeds/aborts when
  the server sends enough consecutive STOPs, or is canceled by the
  caller.

Websocket URL is read from the UNINAVID_WS_URL key in the .env file
(same .env discovery strategy as other cloud nodes).
"""

from __future__ import annotations

# ── Top-level config ──────────────────────────────────────────────────────────
IMAGE_SEND_HZ: float = 5.0   # Hz — how often to push a frame to the websocket
CMD_DURATION_SEC: float = 0.15  # seconds each received action is held on /cmd_vel
CMD_PUBLISH_HZ: float = 10.0  # Hz — publish rate while an action is active
CONSECUTIVE_STOPS_TO_COMPLETE: int = 20  # STOPs in a row → goal succeeded
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import collections
import json
import os
import threading
from typing import Optional

import rclpy
import websockets
from auth_client import AuthProvider
from dotenv import find_dotenv, load_dotenv
from geometry_msgs.msg import Twist
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.node import Node
from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CompressedImage

from innate_cloud_msgs.action import NavigateInstruction

DEFAULT_WS_URL = "ws://localhost:9000"
DEFAULT_AUTH_ISSUER_URL = "https://auth-v1.innate.bot"


# ─────────────────────────────────────────────────────────────────────────────
# Comparison / cmd_vel logic
# ─────────────────────────────────────────────────────────────────────────────

# Action codes sent by the server over the websocket
ACTION_STOP    = 0
ACTION_FORWARD = 1
ACTION_LEFT    = 2
ACTION_RIGHT   = 3

# Velocity lookup table keyed by action code: (linear.x  m/s,  angular.z  rad/s)
_CMD_VELOCITIES: dict[int, tuple[float, float]] = {
    ACTION_STOP:    (0.0,  0.0),
    ACTION_FORWARD: (0.3,  0.0),
    ACTION_LEFT:    (0.0,  0.8),
    ACTION_RIGHT:   (0.0, -0.8),
}


def _compute_cmd_vel(ws_message: str | bytes) -> Optional[Twist]:
    """Parse an integer action code from the websocket and return a Twist.

    The server sends a single integer:
        0 = STOP, 1 = FORWARD, 2 = LEFT, 3 = RIGHT

    Returns ``None`` on unknown / malformed input so the caller skips
    publishing.

    Args:
        ws_message: Raw text or bytes from the websocket.

    Returns:
        A populated ``Twist``, or ``None``.
    """
    if isinstance(ws_message, (bytes, bytearray)):
        ws_message = ws_message.decode(errors="replace")

    try:
        action = int(ws_message.strip())
    except (ValueError, AttributeError):
        return None

    if action not in _CMD_VELOCITIES:
        return None

    lin_x, ang_z = _CMD_VELOCITIES[action]
    twist = Twist()
    twist.linear.x = lin_x
    twist.angular.z = ang_z
    return twist


class UninavidNode(Node):
    """Bridges compressed camera images to a websocket and publishes cmd_vel."""

    def __init__(self) -> None:
        super().__init__("uninavid_node")
        self.get_logger().info("UninavidNode starting")

        # Load .env (walks up from cwd; same pattern used across cloud nodes)
        env_path = find_dotenv(usecwd=True)
        if env_path:
            load_dotenv(env_path)
            self.get_logger().info(f"Loaded .env from {env_path}")
        else:
            self.get_logger().warning(
                "No .env file found; falling back to environment variables"
            )

        # ── Parameters (same env pattern as innate_logger / training_node) ──
        self.declare_parameter(
            "ws_url",
            os.getenv("UNINAVID_WS_URL", DEFAULT_WS_URL),
        )
        self.declare_parameter(
            "service_key",
            os.getenv("INNATE_SERVICE_KEY", ""),
        )
        self.declare_parameter(
            "auth_issuer_url",
            os.getenv("INNATE_AUTH_URL", DEFAULT_AUTH_ISSUER_URL),
        )

        ws_url: str = str(self.get_parameter("ws_url").value)
        service_key: str = str(self.get_parameter("service_key").value)
        auth_issuer: str = str(self.get_parameter("auth_issuer_url").value)

        self.get_logger().info(f"Websocket target: {ws_url}")
        self._ws_url = ws_url

        # ── Auth ─────────────────────────────────────────────────────────────
        if service_key:
            self._auth: Optional[AuthProvider] = AuthProvider(
                issuer_url=auth_issuer,
                service_key=service_key,
            )
            self.get_logger().info("Auth configured (service key present)")
        else:
            self._auth = None
            self.get_logger().warning(
                "No INNATE_SERVICE_KEY — connecting without auth"
            )

        # ── Latest compressed frame (updated from the ROS subscription) ──────
        self._latest_frame: Optional[CompressedImage] = None
        self._frame_lock = threading.Lock()

        # ── Instruction / goal state (shared with the asyncio thread) ────────
        self._instruction: Optional[str] = None
        self._instruction_lock = threading.Lock()
        self._goal_completed = threading.Event()
        self._goal_canceled = threading.Event()

        # Active websocket handle so the action callback can push instructions
        self._ws: Optional[object] = None
        self._ws_lock = threading.Lock()

        # Latest feedback from recv loop: (action_code, consecutive_stops)
        self._latest_feedback: tuple[int, int] = (ACTION_STOP, 0)

        # ── ROS interfaces ────────────────────────────────────────────────────
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
        self._cmd_vel_pub = self.create_publisher(Twist, "/cmd_vel", 10)

        # ── Action server ─────────────────────────────────────────────────────
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

        # ── Asyncio event loop in a background daemon thread ──────────────────
        self._loop = asyncio.new_event_loop()
        self._ws_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="uninavid_ws"
        )
        self._ws_thread.start()

    # ── ROS callbacks ─────────────────────────────────────────────────────────

    def _on_image(self, msg: CompressedImage) -> None:
        """Store the most recent frame so the send loop can pick it up."""
        with self._frame_lock:
            self._latest_frame = msg

    # ── Action server callbacks ───────────────────────────────────────────────

    def _handle_goal(self, goal_request) -> GoalResponse:
        self.get_logger().info(
            f"Goal received: {goal_request.instruction!r}"
        )
        return GoalResponse.ACCEPT

    def _handle_cancel(self, goal_handle) -> CancelResponse:
        self.get_logger().info("Goal cancel requested")
        return CancelResponse.ACCEPT

    def _execute_goal(self, goal_handle):
        """Execute a NavigateInstruction goal.

        Sets the instruction, waits for the ws recv loop to signal
        completion (consecutive STOPs) or cancellation, then returns.
        """
        instruction = goal_handle.request.instruction
        text = f"SET_INSTRUCTION:{instruction}"
        self.get_logger().info(f"Executing goal: {instruction!r}")

        # Reset events
        self._goal_completed.clear()
        self._goal_canceled.clear()

        # Store instruction and push to websocket if connected
        with self._instruction_lock:
            self._instruction = text
        with self._ws_lock:
            ws = self._ws
        if ws is not None:
            asyncio.run_coroutine_threadsafe(ws.send(text), self._loop)

        result = NavigateInstruction.Result()
        feedback = NavigateInstruction.Feedback()

        # Poll until the ws recv loop signals done, or the goal is canceled
        while rclpy.ok():
            if goal_handle.is_cancel_requested:
                goal_handle.canceled()
                self.get_logger().info("Goal canceled by client")
                # Tell the server to clear
                with self._instruction_lock:
                    self._instruction = None
                with self._ws_lock:
                    ws = self._ws
                if ws is not None:
                    asyncio.run_coroutine_threadsafe(
                        ws.send("SET_INSTRUCTION:null"), self._loop
                    )
                result.success = False
                result.message = "Canceled"
                return result

            if self._goal_completed.wait(timeout=0.1):
                goal_handle.succeed()
                self.get_logger().info("Goal succeeded (consecutive STOPs)")
                result.success = True
                result.message = "Navigation completed"
                return result

            # Publish feedback from whatever the recv loop last reported
            action_code, stops = self._latest_feedback
            feedback.latest_action = action_code
            feedback.consecutive_stops = stops
            goal_handle.publish_feedback(feedback)

        # Node shutting down
        goal_handle.abort()
        result.success = False
        result.message = "Node shutting down"
        return result

    # ── Asyncio helpers (all run inside _ws_thread) ───────────────────────────

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._ws_lifecycle())

    async def _ws_lifecycle(self) -> None:
        """Connect with auto-reconnect and drive the send + receive loops."""
        send_interval = 1.0 / IMAGE_SEND_HZ

        while rclpy.ok():
            try:
                self.get_logger().info(f"Connecting to {self._ws_url} …")
                extra_headers = {}
                if self._auth is not None:
                    extra_headers["Authorization"] = f"Bearer {self._auth.token}"
                async with websockets.connect(
                    self._ws_url,
                    extra_headers=extra_headers,
                ) as ws:
                    self.get_logger().info("Websocket connected")

                    with self._ws_lock:
                        self._ws = ws

                    # Re-send the latest instruction on every (re)connect
                    with self._instruction_lock:
                        instruction = self._instruction
                    if instruction is not None:
                        await ws.send(instruction)
                        self.get_logger().info(f"Sent on connect: {instruction!r}")

                    send_task = asyncio.create_task(
                        self._send_loop(ws, send_interval)
                    )
                    recv_task = asyncio.create_task(self._recv_loop(ws))

                    done, pending = await asyncio.wait(
                        {send_task, recv_task},
                        return_when=asyncio.FIRST_EXCEPTION,
                    )
                    for task in pending:
                        task.cancel()
                    # Re-raise the first exception so we fall into the retry
                    for task in done:
                        exc = task.exception()
                        if exc is not None:
                            raise exc

            except websockets.ConnectionClosed as exc:
                self.get_logger().warning(
                    f"Websocket closed ({exc}); retrying in 2 s …"
                )
            except OSError as exc:
                self.get_logger().warning(
                    f"Websocket OS error ({exc}); retrying in 2 s …"
                )
            except Exception as exc:  # noqa: BLE001
                self.get_logger().error(
                    f"Websocket unexpected error ({exc}); retrying in 2 s …"
                )
            finally:
                with self._ws_lock:
                    self._ws = None

            await asyncio.sleep(2.0)

    async def _send_loop(
        self,
        ws: websockets.WebSocketClientProtocol,
        interval: float,
    ) -> None:
        """Push the latest compressed frame to the websocket at IMAGE_SEND_HZ.

        Each message is:
            <JSON header bytes> LF <raw image bytes>

        The JSON header contains format, stamp_sec, and stamp_nanosec so
        the server can decode the frame without out-of-band framing.
        """
        while True:
            await asyncio.sleep(interval)

            with self._frame_lock:
                frame = self._latest_frame

            if frame is None:
                continue

            header = json.dumps(
                {
                    "type": "image",
                    "format": frame.format,
                    "stamp_sec": frame.header.stamp.sec,
                    "stamp_nanosec": frame.header.stamp.nanosec,
                }
            ).encode()
            payload: bytes = header + b"\n" + bytes(frame.data)
            await ws.send(payload)

    async def _recv_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Receive messages from the websocket and publish cmd_vel.

        Only the latest 4 actions are kept; anything older is discarded.
        Each action is published continuously at CMD_PUBLISH_HZ for
        CMD_DURATION_SEC seconds, then a zero-velocity stop is sent.
        A new message arriving mid-action cancels the previous one immediately.

        When CONSECUTIVE_STOPS_TO_COMPLETE consecutive STOP actions are
        received the active goal is completed via ``_goal_completed`` event.
        """
        MAX_QUEUED_ACTIONS = 4
        action_queue: collections.deque[Twist] = collections.deque(maxlen=MAX_QUEUED_ACTIONS)
        _stop = Twist()  # all-zero, used to halt motion after each action
        publish_interval = 1.0 / CMD_PUBLISH_HZ
        consecutive_stops = 0

        async def _hold(twist: Twist) -> None:
            deadline = asyncio.get_event_loop().time() + CMD_DURATION_SEC
            while asyncio.get_event_loop().time() < deadline:
                self._cmd_vel_pub.publish(twist)
                await asyncio.sleep(publish_interval)
            self._cmd_vel_pub.publish(_stop)  # explicit stop when duration expires

        async def _drain_queue() -> None:
            """Execute queued actions one at a time."""
            while action_queue:
                twist = action_queue.popleft()
                await _hold(twist)

        drain_task: Optional[asyncio.Task] = None

        async for raw in ws:
            twist = _compute_cmd_vel(raw)
            if twist is None:
                continue

            # Parse action code for tracking / feedback
            if isinstance(raw, (bytes, bytearray)):
                raw_str = raw.decode(errors="replace")
            else:
                raw_str = raw
            try:
                action = int(raw_str.strip())
            except (ValueError, AttributeError):
                action = -1

            if action == ACTION_STOP:
                consecutive_stops += 1
            else:
                consecutive_stops = 0

            # Publish action feedback to the goal handle (thread-safe store)
            self._latest_feedback = (action, consecutive_stops)

            if consecutive_stops >= CONSECUTIVE_STOPS_TO_COMPLETE:
                consecutive_stops = 0
                self.get_logger().info(
                    f"{CONSECUTIVE_STOPS_TO_COMPLETE} consecutive STOPs — "
                    "signalling goal complete"
                )
                with self._instruction_lock:
                    self._instruction = None
                await ws.send("SET_INSTRUCTION:null")
                self._goal_completed.set()
                await ws.close()
                continue

            # Cancel any in-flight drain before rebuilding the queue
            if drain_task and not drain_task.done():
                drain_task.cancel()
                try:
                    await drain_task
                except asyncio.CancelledError:
                    pass
                action_queue.clear()

            action_queue.append(twist)
            # Only start a new drain if one isn't already running
            if drain_task is None or drain_task.done():
                drain_task = asyncio.create_task(_drain_queue())


def main(args=None) -> None:
    rclpy.init(args=args)
    node = UninavidNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
