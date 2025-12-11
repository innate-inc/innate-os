#!/usr/bin/env python3
"""
MoondreamInferenceBeta Primitive

Connects to a Moondream inference server, streams sensor data (camera, odom),
and executes waypoint commands received from the server using direct cmd_vel control.

This primitive runs continuously until the task completes, is cancelled, or receives
a kill command from the server.
"""

import asyncio
import threading
import time
import base64
import math
import json
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any

from geometry_msgs.msg import Twist
from brain_client.primitive_types import Primitive, PrimitiveResult, RobotStateType


# ============================================================================
# Protocol Definitions (from shared_protocol.py)
# ============================================================================

MSG_TYPE_SENSOR_DATA = "sensor_data"
MSG_TYPE_WAYPOINT_COMMAND = "waypoint_command"
MSG_TYPE_TASK_START = "task_start"
MSG_TYPE_TASK_STOP = "task_stop"
MSG_TYPE_HEARTBEAT = "heartbeat"
MSG_TYPE_STATUS = "status"
MSG_TYPE_ERROR = "error"
MSG_TYPE_KILL = "kill"

DEFAULT_INFERENCE_PORT = 8780


@dataclass
class OdometryProto:
    """Robot odometry state for protocol."""
    x: float
    y: float
    yaw: float
    timestamp: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'OdometryProto':
        return cls(**d)


@dataclass
class SensorData:
    """Sensor data streamed from robot to server."""
    image_b64: str
    image_width: int
    image_height: int
    odom: OdometryProto
    head_pitch_deg: float
    timestamp: float
    seq: int

    def to_json(self) -> str:
        return json.dumps({
            "type": MSG_TYPE_SENSOR_DATA,
            "image_b64": self.image_b64,
            "image_width": self.image_width,
            "image_height": self.image_height,
            "odom": self.odom.to_dict(),
            "head_pitch_deg": self.head_pitch_deg,
            "timestamp": self.timestamp,
            "seq": self.seq
        })


@dataclass
class Waypoint:
    """A single waypoint in world (odom) frame."""
    x: float
    y: float

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'Waypoint':
        return cls(**d)


@dataclass
class WaypointCommand:
    """Waypoint command from server to robot."""
    waypoints: List[Waypoint]
    timestamp: float
    task_label: str
    seq: int

    @classmethod
    def from_dict(cls, d: dict) -> 'WaypointCommand':
        return cls(
            waypoints=[Waypoint.from_dict(w) for w in d["waypoints"]],
            timestamp=d["timestamp"],
            task_label=d["task_label"],
            seq=d["seq"]
        )


@dataclass
class TaskStart:
    """Command to start a task."""
    task_label: str
    num_waypoints: int
    inference_interval_ms: int

    @classmethod
    def from_dict(cls, d: dict) -> 'TaskStart':
        return cls(
            task_label=d["task_label"],
            num_waypoints=d["num_waypoints"],
            inference_interval_ms=d["inference_interval_ms"]
        )


@dataclass
class TaskStop:
    """Command to stop current task."""
    reason: str = "user_stop"

    @classmethod
    def from_dict(cls, d: dict) -> 'TaskStop':
        return cls(reason=d.get("reason", "user_stop"))


@dataclass
class Status:
    """Status update from robot or server."""
    source: str
    state: str
    message: str
    data: Optional[Dict[str, Any]] = None

    def to_json(self) -> str:
        return json.dumps({
            "type": MSG_TYPE_STATUS,
            "source": self.source,
            "state": self.state,
            "message": self.message,
            "data": self.data or {}
        })


@dataclass
class Heartbeat:
    """Heartbeat to keep connection alive."""
    timestamp: float


@dataclass
class Kill:
    """Emergency kill command."""
    pass


def parse_message(json_str: str) -> Any:
    """Parse a JSON message and return the appropriate dataclass."""
    d = json.loads(json_str)
    msg_type = d.get("type")

    if msg_type == MSG_TYPE_WAYPOINT_COMMAND:
        return WaypointCommand.from_dict(d)
    elif msg_type == MSG_TYPE_TASK_START:
        return TaskStart.from_dict(d)
    elif msg_type == MSG_TYPE_TASK_STOP:
        return TaskStop.from_dict(d)
    elif msg_type == MSG_TYPE_STATUS:
        return Status(
            source=d["source"],
            state=d["state"],
            message=d["message"],
            data=d.get("data")
        )
    elif msg_type == MSG_TYPE_HEARTBEAT:
        return Heartbeat(timestamp=d["timestamp"])
    elif msg_type == MSG_TYPE_KILL:
        return Kill()
    else:
        return d


# ============================================================================
# Waypoint Executor
# ============================================================================

class DirectWaypointExecutor:
    """
    Direct waypoint execution using proportional control on /cmd_vel.
    """

    LINEAR_SPEED = 0.3
    ANGULAR_SPEED = 0.6
    DISTANCE_TOLERANCE = 0.15
    ANGLE_TOLERANCE = 0.15
    TURN_FIRST_THRESHOLD = 0.4

    def __init__(self, logger, cmd_vel_pub):
        self.logger = logger
        self.cmd_vel_pub = cmd_vel_pub

        self.is_executing = False
        self.current_waypoints: List[Waypoint] = []
        self.current_waypoint_idx = 0

        self.robot_x = 0.0
        self.robot_y = 0.0
        self.robot_yaw = 0.0

        self.lock = threading.Lock()

    def update_waypoints(self, waypoints: List[Waypoint], robot_x: float, robot_y: float, robot_yaw: float):
        """Update waypoints to follow."""
        with self.lock:
            self.current_waypoints = waypoints
            self.current_waypoint_idx = 0
            self.robot_x = robot_x
            self.robot_y = robot_y
            self.robot_yaw = robot_yaw

            if waypoints:
                self.is_executing = True
                wp = waypoints[0]
                self.logger.info(f"[WaypointExecutor] {len(waypoints)} waypoints, first=({wp.x:.2f}, {wp.y:.2f})")
            else:
                self.is_executing = False

    def update_pose(self, robot_x: float, robot_y: float, robot_yaw: float):
        """Update current robot pose from odometry."""
        with self.lock:
            self.robot_x = robot_x
            self.robot_y = robot_y
            self.robot_yaw = robot_yaw

    def _normalize_angle(self, angle: float) -> float:
        """Normalize angle to [-pi, pi]."""
        while angle > math.pi:
            angle -= 2 * math.pi
        while angle < -math.pi:
            angle += 2 * math.pi
        return angle

    def control_step(self):
        """Single control loop iteration - compute and publish cmd_vel."""
        with self.lock:
            if not self.is_executing or not self.current_waypoints:
                return False

            if self.current_waypoint_idx >= len(self.current_waypoints):
                self._stop_robot()
                self.is_executing = False
                self.logger.info("[WaypointExecutor] All waypoints reached!")
                return False

            wp = self.current_waypoints[self.current_waypoint_idx]

            dx = wp.x - self.robot_x
            dy = wp.y - self.robot_y
            distance = math.sqrt(dx * dx + dy * dy)

            if distance < self.DISTANCE_TOLERANCE:
                self.current_waypoint_idx += 1
                self.logger.info(f"[WaypointExecutor] Waypoint {self.current_waypoint_idx} reached")
                if self.current_waypoint_idx >= len(self.current_waypoints):
                    self._stop_robot()
                    self.is_executing = False
                    self.logger.info("[WaypointExecutor] All waypoints completed!")
                    return False
                return True

            desired_yaw = math.atan2(dy, dx)
            angle_error = self._normalize_angle(desired_yaw - self.robot_yaw)

            twist = Twist()

            if abs(angle_error) > self.TURN_FIRST_THRESHOLD:
                twist.linear.x = 0.0
                twist.angular.z = self.ANGULAR_SPEED * (1.0 if angle_error > 0 else -1.0)
            else:
                twist.linear.x = self.LINEAR_SPEED
                twist.angular.z = 2.0 * angle_error
                twist.angular.z = max(-self.ANGULAR_SPEED, min(self.ANGULAR_SPEED, twist.angular.z))

            self.cmd_vel_pub.publish(twist)
            return True

    def _stop_robot(self):
        """Send zero velocity command."""
        twist = Twist()
        self.cmd_vel_pub.publish(twist)

    def stop(self):
        """Stop current execution."""
        with self.lock:
            self.is_executing = False
            self.current_waypoints = []
            self.current_waypoint_idx = 0
        self._stop_robot()
        self.logger.info("[WaypointExecutor] Execution stopped")


# ============================================================================
# Main Primitive
# ============================================================================

class MoondreamInferenceBeta(Primitive):
    """
    Primitive that connects to a Moondream inference server, streams sensor data,
    and executes waypoint commands.
    """

    def __init__(self, logger):
        super().__init__(logger)

        # Robot state from update_robot_state
        self.last_image_b64: Optional[str] = None
        self.last_odom: Optional[Dict] = None

        # Runtime state
        self._cancel_requested = threading.Event()
        self._task_active = False
        self._killed = False
        self._ws = None
        self._ws_loop = None
        self._waypoint_executor: Optional[DirectWaypointExecutor] = None
        self._cmd_vel_pub = None
        self._control_timer = None
        self._seq = 0

    @property
    def name(self):
        return "moondream_inference_beta"

    def get_required_robot_states(self) -> list[RobotStateType]:
        """Declare that this primitive needs camera image and odometry."""
        return [RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64, RobotStateType.LAST_ODOM]

    def update_robot_state(self, **kwargs):
        """Store the latest robot state."""
        if RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64.value in kwargs:
            self.last_image_b64 = kwargs[RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64.value]

        if RobotStateType.LAST_ODOM.value in kwargs:
            self.last_odom = kwargs[RobotStateType.LAST_ODOM.value]

    def guidelines(self):
        return (
            "Use to run Moondream visual inference for navigation tasks. "
            "This connects to an inference server, streams camera and odometry data, "
            "and executes waypoint commands received from the server. "
            "Provide the server_address (IP), task_label (description of what to do), "
            "and optionally server_port (default 8780), stream_hz (default 10), "
            "and timeout_seconds (default 300)."
        )

    def guidelines_when_running(self):
        return (
            "Watch for connection issues or navigation failures. "
            "The robot will follow waypoints sent by the inference server. "
            "Cancel if the robot appears stuck or heading in the wrong direction."
        )

    def execute(
        self,
        server_address: str,
        task_label: str,
        server_port: int = DEFAULT_INFERENCE_PORT,
        stream_hz: float = 10.0,
        timeout_seconds: float = 300.0
    ):
        """
        Connect to inference server and execute waypoint commands.

        Args:
            server_address: IP address of the inference server
            task_label: Description of the navigation task
            server_port: Port of the inference server (default 8780)
            stream_hz: Rate to stream sensor data (default 10 Hz)
            timeout_seconds: Maximum execution time (default 300s)

        Returns:
            tuple: (result_message, PrimitiveResult)
        """
        self.logger.info(
            f"[MoondreamInferenceBeta] Starting: server={server_address}:{server_port}, "
            f"task={task_label}, stream_hz={stream_hz}"
        )

        if not self.node:
            self.logger.error("[MoondreamInferenceBeta] No ROS node available")
            return "Primitive not initialized (no ROS node)", PrimitiveResult.FAILURE

        # Reset state
        self._cancel_requested.clear()
        self._task_active = False
        self._killed = False
        self._seq = 0

        # Create cmd_vel publisher
        self._cmd_vel_pub = self.node.create_publisher(Twist, '/cmd_vel', 10)
        self._waypoint_executor = DirectWaypointExecutor(self.logger, self._cmd_vel_pub)

        # Create control timer for waypoint execution (20 Hz)
        self._control_timer = self.node.create_timer(0.05, self._control_loop_callback)

        try:
            # Import websockets here to avoid import errors if not installed
            import websockets

            # Run the WebSocket client
            result = self._run_websocket_session(
                server_address, server_port, task_label, stream_hz, timeout_seconds
            )

            return result

        except ImportError:
            self.logger.error("[MoondreamInferenceBeta] websockets library not installed")
            return "websockets library not installed", PrimitiveResult.FAILURE
        except Exception as e:
            self.logger.error(f"[MoondreamInferenceBeta] Error: {e}")
            return f"Error: {e}", PrimitiveResult.FAILURE
        finally:
            # Cleanup
            self._cleanup()

    def _control_loop_callback(self):
        """Timer callback for waypoint control loop."""
        if self._waypoint_executor:
            self._waypoint_executor.control_step()

    def _run_websocket_session(
        self,
        server_address: str,
        server_port: int,
        task_label: str,
        stream_hz: float,
        timeout_seconds: float
    ):
        """Run the WebSocket session synchronously."""
        import websockets
        import asyncio

        # Create event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._ws_loop = loop

        try:
            result = loop.run_until_complete(
                self._websocket_session(
                    server_address, server_port, task_label, stream_hz, timeout_seconds
                )
            )
            return result
        finally:
            loop.close()

    async def _websocket_session(
        self,
        server_address: str,
        server_port: int,
        task_label: str,
        stream_hz: float,
        timeout_seconds: float
    ):
        """Async WebSocket session."""
        import websockets

        server_url = f"ws://{server_address}:{server_port}"
        start_time = time.time()
        stream_interval = 1.0 / stream_hz

        self.logger.info(f"[MoondreamInferenceBeta] Connecting to {server_url}...")

        try:
            async with websockets.connect(server_url, max_size=10 * 1024 * 1024) as ws:
                self._ws = ws
                self.logger.info("[MoondreamInferenceBeta] Connected!")

                # Send initial status
                await ws.send(Status(
                    source="robot",
                    state="connected",
                    message=f"Robot connected, ready for task: {task_label}"
                ).to_json())

                self._send_feedback(f"Connected to inference server at {server_address}")

                last_stream_time = 0.0

                while True:
                    # Check cancellation
                    if self._cancel_requested.is_set():
                        self.logger.info("[MoondreamInferenceBeta] Cancelled by user")
                        return "Inference cancelled", PrimitiveResult.CANCELLED

                    # Check kill
                    if self._killed:
                        self.logger.info("[MoondreamInferenceBeta] Killed by server")
                        return "Killed by server", PrimitiveResult.CANCELLED

                    # Check timeout
                    elapsed = time.time() - start_time
                    if elapsed > timeout_seconds:
                        self.logger.info("[MoondreamInferenceBeta] Timeout reached")
                        return f"Timeout after {timeout_seconds}s", PrimitiveResult.FAILURE

                    # Stream sensor data at configured rate
                    now = time.time()
                    if now - last_stream_time >= stream_interval:
                        await self._stream_sensor_data()
                        last_stream_time = now

                    # Receive messages with short timeout
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=0.05)
                        result = await self._handle_server_message(message)
                        if result is not None:
                            return result
                    except asyncio.TimeoutError:
                        pass

        except websockets.exceptions.ConnectionClosed as e:
            self.logger.warn(f"[MoondreamInferenceBeta] Connection closed: {e}")
            return f"Connection closed: {e}", PrimitiveResult.FAILURE
        except Exception as e:
            self.logger.error(f"[MoondreamInferenceBeta] WebSocket error: {e}")
            return f"WebSocket error: {e}", PrimitiveResult.FAILURE

    async def _stream_sensor_data(self):
        """Stream current sensor data to server."""
        if self._ws is None:
            return

        if self.last_image_b64 is None or self.last_odom is None:
            if self._seq % 50 == 0:
                missing = []
                if self.last_image_b64 is None:
                    missing.append("camera")
                if self.last_odom is None:
                    missing.append("odom")
                self.logger.warn(f"[MoondreamInferenceBeta] Waiting for: {', '.join(missing)}")
            self._seq += 1
            return

        # Extract odom values
        odom = self.last_odom
        pos = odom["pose"]["pose"]["position"]
        ori = odom["pose"]["pose"]["orientation"]

        # Calculate yaw from quaternion
        siny_cosp = 2.0 * (ori["w"] * ori["z"] + ori["x"] * ori["y"])
        cosy_cosp = 1.0 - 2.0 * (ori["y"] ** 2 + ori["z"] ** 2)
        yaw = math.atan2(siny_cosp, cosy_cosp)

        # Update waypoint executor with current pose
        if self._waypoint_executor:
            self._waypoint_executor.update_pose(pos["x"], pos["y"], yaw)

        odom_proto = OdometryProto(
            x=pos["x"],
            y=pos["y"],
            yaw=yaw,
            timestamp=odom["header"]["stamp"]["sec"] + odom["header"]["stamp"]["nanosec"] * 1e-9
        )

        self._seq += 1
        sensor_data = SensorData(
            image_b64=self.last_image_b64,
            image_width=640,
            image_height=480,
            odom=odom_proto,
            head_pitch_deg=-10.0,
            timestamp=time.time(),
            seq=self._seq
        )

        try:
            await self._ws.send(sensor_data.to_json())

            if self._seq == 1:
                self.logger.info("[MoondreamInferenceBeta] Streaming started!")
                self._send_feedback("Streaming sensor data to server")
            elif self._seq % 100 == 0:
                self.logger.info(f"[MoondreamInferenceBeta] Streamed {self._seq} frames")
        except Exception as e:
            self.logger.warn(f"[MoondreamInferenceBeta] Failed to send: {e}")

    async def _handle_server_message(self, message: str):
        """
        Process message from inference server.
        Returns a result tuple if execution should end, None otherwise.
        """
        try:
            parsed = parse_message(message)

            if isinstance(parsed, WaypointCommand):
                self.logger.info(
                    f"[MoondreamInferenceBeta] Waypoint command #{parsed.seq} "
                    f"with {len(parsed.waypoints)} waypoints"
                )

                if not self._killed and self._task_active and self._waypoint_executor:
                    if self.last_odom:
                        pos = self.last_odom["pose"]["pose"]["position"]
                        ori = self.last_odom["pose"]["pose"]["orientation"]
                        siny_cosp = 2.0 * (ori["w"] * ori["z"] + ori["x"] * ori["y"])
                        cosy_cosp = 1.0 - 2.0 * (ori["y"] ** 2 + ori["z"] ** 2)
                        yaw = math.atan2(siny_cosp, cosy_cosp)

                        self._waypoint_executor.update_waypoints(
                            parsed.waypoints, pos["x"], pos["y"], yaw
                        )

            elif isinstance(parsed, TaskStart):
                self.logger.info(f"[MoondreamInferenceBeta] Task started: {parsed.task_label}")
                self._task_active = True
                self._killed = False
                self._send_feedback(f"Task started: {parsed.task_label}")

            elif isinstance(parsed, TaskStop):
                self.logger.info(f"[MoondreamInferenceBeta] Task stopped: {parsed.reason}")
                self._task_active = False
                if self._waypoint_executor:
                    self._waypoint_executor.stop()
                return f"Task stopped: {parsed.reason}", PrimitiveResult.SUCCESS

            elif isinstance(parsed, Kill):
                self.logger.error("[MoondreamInferenceBeta] !!! KILL COMMAND !!!")
                self._killed = True
                self._task_active = False
                if self._waypoint_executor:
                    self._waypoint_executor.stop()
                return "Kill command received", PrimitiveResult.CANCELLED

            elif isinstance(parsed, Status):
                self.logger.info(f"[MoondreamInferenceBeta] Server: {parsed.message}")

            elif isinstance(parsed, Heartbeat):
                pass

        except Exception as e:
            self.logger.error(f"[MoondreamInferenceBeta] Error handling message: {e}")

        return None

    def _cleanup(self):
        """Clean up resources."""
        if self._waypoint_executor:
            self._waypoint_executor.stop()
            self._waypoint_executor = None

        if self._control_timer and self.node:
            self.node.destroy_timer(self._control_timer)
            self._control_timer = None

        if self._cmd_vel_pub and self.node:
            self.node.destroy_publisher(self._cmd_vel_pub)
            self._cmd_vel_pub = None

        self._ws = None
        self._ws_loop = None

    def cancel(self):
        """Cancel the inference operation."""
        self.logger.info("[MoondreamInferenceBeta] Cancel requested")
        self._cancel_requested.set()

        if self._waypoint_executor:
            self._waypoint_executor.stop()

        return "Moondream inference cancellation requested"

