"""ROS 2 node impersonating the MARS hardware drivers with virtual_mars_core.
Topic/service contracts mirror the real stack (mars_bringup, rplidar launch,
mars_arm, mars_cam's stereo depth estimator) so brain_client, Nav2 and AMCL
run unchanged -- see README "Virtual MARS driver".

  pub /odom                                       nav_msgs/Odometry @30Hz (pose only, like bringup.py)
  pub TF odom->base_link                          @30Hz
  pub /scan                                       sensor_msgs/LaserScan @6Hz, frame base_laser
  pub /mars/main_camera/left/image_raw/compressed sensor_msgs/CompressedImage @7.5Hz (lazy)
  pub /mars/arm/image_raw/compressed              sensor_msgs/CompressedImage @5Hz (lazy)
  pub /mars/main_camera/depth/image_rect_raw      sensor_msgs/Image 16SC1 mm @8Hz (lazy)
  pub /mars/main_camera/points                    sensor_msgs/PointCloud2 xyz @8Hz (lazy)
  pub /mars/main_camera/left/camera_info          sensor_msgs/CameraInfo @8Hz
  pub /mars/arm/state                             sensor_msgs/JointState (joint1..6, rad) @20Hz
  pub /joint_states                               sensor_msgs/JointState (+joint_head) @30Hz
  pub /mars/head/current_position                 std_msgs/String (JSON, degrees) @10Hz
  sub /cmd_vel                                    geometry_msgs/Twist
  sub /mars/arm/commands                          std_msgs/Float64MultiArray (6x rad, best-effort)
  sub /mars/head/set_position                     std_msgs/Int32 (degrees)
  srv /mars/arm/goto_js, /goto_js_v2, /goto_js_trajectory (mars_msgs, if built)
  srv /mars/arm/torque_on|torque_off|reboot       std_srvs/Trigger (no-ops)
  srv /mars/head/set_ai_position                  std_srvs/Trigger (-20 deg)

The URDF's static frames (base_footprint, base_laser, camera_optical_frame,
arm links) come from robot_state_publisher fed by /joint_states -- launch it
alongside this node with the same mars.urdf. AMCL owns map->odom.

The world itself runs in the world server (world_server.py); this node is a
thin RPC client. VIRTUAL_MARS_REMOTE picks the endpoint (default
127.0.0.1:8799, started by sim_driver.launch.py).
"""

import contextlib
import io
import json
import math
import os
import threading
import time
from functools import lru_cache
from types import SimpleNamespace

import numpy as np
import rclpy
from geometry_msgs.msg import TransformStamped, Twist
from nav_msgs.msg import Odometry
from PIL import Image as PILImage
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, CompressedImage, Image, JointState, LaserScan, PointCloud2, PointField
from std_msgs.msg import Empty, Float64MultiArray, Int32, String
from std_srvs.srv import Trigger
from tf2_ros import TransformBroadcaster

from .constants import CAMERA_FOVY, CAMERA_HEIGHT, CAMERA_WIDTH
from .remote_world import RemoteWorld

try:
    from mars_msgs.srv import GotoJS, GotoJSTrajectory
except ImportError:  # ros2_ws not sourced/built -- topic interface still works
    GotoJS = GotoJSTrajectory = None

MAIN_CAMERA_FPS = 7.5  # main_camera_driver: 15fps capture, JPEG every 2nd frame
WRIST_CAMERA_FPS = 5.0  # arm_camera_driver: 30fps capture, JPEG every 6th frame
DEPTH_FPS = 8.0  # stereo_depth_estimator max_fps
ODOM_HZ = 30.0  # bringup.py odom_frequency
SCAN_HZ = 6.0  # lidar.launch.py throttle
JOINT_STATE_HZ = 30.0
ARM_STATE_HZ = 20.0
HEAD_POSITION_HZ = 10.0

LIDAR_N_RAYS = 360
LIDAR_RANGE_MIN = 0.15  # rplidar A-series
LIDAR_RANGE_MAX = 12.0
HEAD_MIN_DEG, HEAD_MAX_DEG = -25.0, 25.0  # arm_config.yaml joint_7 limits
POINTS_SUBSAMPLE = 4  # 640x480 -> 160x120 cloud; costmap voxel-filters anyway
# The real stereo pipeline clamps depth to [0.25, 2.0]m (depth_clamp filter in
# stereo_depth_estimator.yaml) -- without the near clamp the sim camera sees
# the robot's own arm, which STVL then marks as an obstacle at the footprint.
DEPTH_MIN_M = 0.25
DEPTH_MAX_M = 2.0

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6"]

# Pinhole intrinsics implied by the render (square pixels, principal point
# centered): fy from the vertical FOV, fx = fy.
FOCAL = CAMERA_HEIGHT / (2 * math.tan(math.radians(CAMERA_FOVY) / 2))
CX, CY = CAMERA_WIDTH / 2, CAMERA_HEIGHT / 2

WORLD_DEFAULT_ENDPOINT = "127.0.0.1:8799"


@lru_cache(maxsize=4)
def _points_grid(h: int, w: int, step: int) -> tuple[np.ndarray, np.ndarray]:
    vs, us = np.mgrid[0:h:step, 0:w:step]
    return vs.astype(np.float32), us.astype(np.float32)


class VirtualMarsNode(Node):
    def __init__(self) -> None:
        super().__init__("virtual_mars")
        endpoint = os.environ.get("VIRTUAL_MARS_REMOTE", "").strip() or WORLD_DEFAULT_ENDPOINT
        host, _, port = endpoint.partition(":")
        self.sim = RemoteWorld(host, int(port or "8799"))
        self.get_logger().info(f"waiting for the world server at {endpoint}...")
        self.sim.wait_ready(timeout=180.0)
        self.get_logger().info(f"world server connected at {endpoint}")
        self._lock = threading.Lock()
        # Queued (from, to, duration) segments consumed by the trajectory
        # loop; _traj_req is the waiting goto_js* call's per-request
        # completion token (concurrent calls can't race each other's result).
        self._segments: list[tuple[dict, dict, float]] = []
        self._segment_started: float | None = None
        self._traj_req: SimpleNamespace | None = None
        self._last_stream_at = 0.0  # /mars/arm/commands ramp pacing
        self._stream_interval = 0.1  # EMA of the stream's cadence (see _on_arm_commands)

        self._tf = TransformBroadcaster(self)
        # State topics publish KEEP_LAST(1): a hop that buffers more than the
        # newest sample turns a slow consumer into permanent display lag.
        self._odom_pub = self.create_publisher(Odometry, "/odom", 1)
        self._scan_pub = self.create_publisher(LaserScan, "/scan", qos_profile_sensor_data)
        self._main_pub = self.create_publisher(
            CompressedImage, "/mars/main_camera/left/image_raw/compressed", qos_profile_sensor_data
        )
        self._wrist_pub = self.create_publisher(
            CompressedImage, "/mars/arm/image_raw/compressed", qos_profile_sensor_data
        )
        # Raw variants: what webrtc_streamer (webapp video) encodes.
        self._main_raw_pub = self.create_publisher(Image, "/mars/main_camera/left/image_raw", qos_profile_sensor_data)
        self._wrist_raw_pub = self.create_publisher(Image, "/mars/arm/image_raw", qos_profile_sensor_data)
        self._depth_pub = self.create_publisher(
            Image, "/mars/main_camera/depth/image_rect_raw", qos_profile_sensor_data
        )
        self._points_pub = self.create_publisher(PointCloud2, "/mars/main_camera/points", qos_profile_sensor_data)
        self._caminfo_pub = self.create_publisher(CameraInfo, "/mars/main_camera/left/camera_info", 10)
        self._arm_state_pub = self.create_publisher(JointState, "/mars/arm/state", 1)
        self._joint_states_pub = self.create_publisher(JointState, "/joint_states", 1)
        self._head_pub = self.create_publisher(String, "/mars/head/current_position", 1)

        # Latched robot identity: clients (webapp) pick their camera view
        # implementation from this -- rendered view for sim, WebRTC for real.
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._info_pub = self.create_publisher(String, "/robot_info", latched)
        info = String()
        info.data = json.dumps({"simulated": True, "driver": "virtual_mars", "physics": "mujoco"})
        self._info_pub.publish(info)

        # Camera roster, mirroring webrtc_streamer's /webrtc/active_streams
        # payload (2s cadence there): the webapp builds its camera switcher
        # from `cameras`. "orbit" is sim-only -- a free chase view the
        # SimSession renders; a real robot has no such camera.
        self._streams_pub = self.create_publisher(String, "/webrtc/active_streams", 10)
        self.create_timer(2.0, self._publish_active_streams)

        # Depth 1: a deep queue replays stale Twists after executor hiccups.
        self.create_subscription(Twist, "/cmd_vel", self._on_cmd_vel, 1)
        self.create_subscription(
            Float64MultiArray, "/mars/arm/commands", self._on_arm_commands, qos_profile_sensor_data
        )
        self.create_subscription(Int32, "/mars/head/set_position", self._on_head_position, 10)
        # Sim-only convenience (no real-robot equivalent): respawn at the
        # spawn pose, used by sim/viewer's Reset button in connected mode.
        self.create_subscription(Empty, "/virtual_mars/reset", self._on_reset, 10)

        services = ReentrantCallbackGroup()  # goto services block; don't starve timers
        if GotoJS is not None:
            self.create_service(GotoJS, "/mars/arm/goto_js", self._on_goto_js, callback_group=services)
            self.create_service(GotoJS, "/mars/arm/goto_js_v2", self._on_goto_js, callback_group=services)
            self.create_service(
                GotoJSTrajectory, "/mars/arm/goto_js_trajectory", self._on_goto_js_trajectory, callback_group=services
            )
        else:
            self.get_logger().warning("mars_msgs not importable -- /mars/arm/goto_js* services disabled")
        for name in ("torque_on", "torque_off", "reboot", "fix_error"):
            self.create_service(Trigger, f"/mars/arm/{name}", self._on_trigger_noop, callback_group=services)
        self.create_service(Trigger, "/mars/head/set_ai_position", self._on_head_ai_position, callback_group=services)

        self.create_timer(1.0 / ODOM_HZ, self._publish_odom)
        self.create_timer(1.0 / SCAN_HZ, self._publish_scan)
        self.create_timer(1.0 / DEPTH_FPS, self._publish_camera_info)
        self.create_timer(1.0 / JOINT_STATE_HZ, self._publish_joint_states)
        self.create_timer(1.0 / ARM_STATE_HZ, self._publish_arm_state)
        self.create_timer(1.0 / HEAD_POSITION_HZ, self._publish_head)

        threading.Thread(target=self._trajectory_loop, daemon=True).start()
        # Rendering off the executor: a software-GL render takes ~100ms+ and
        # would starve the timers and /cmd_vel.
        threading.Thread(target=self._render_loop, daemon=True).start()
        self.get_logger().info("virtual MARS up: odom + scan + cameras + depth/points + arm/head")

    # --- inputs ---

    def _rpc_safe(self, what: str, fn) -> None:
        """Run an RPC-backed command, tolerating a briefly-away world server
        (restart window): the command is dropped with a throttled warning
        instead of the callback exception killing the executor."""
        try:
            fn()
        except (OSError, RuntimeError) as exc:
            self.get_logger().warning(f"{what} dropped: world server unavailable ({exc})", throttle_duration_sec=5.0)

    def _on_cmd_vel(self, msg: Twist) -> None:
        with self._lock:
            self._rpc_safe("cmd_vel", lambda: self.sim.set_cmd_vel(msg.linear.x, msg.angular.z))

    def _fail_active_traj(self) -> None:
        """Preempt the queued trajectory: wake its waiting goto_js* call with
        failure (real arm semantics -- the pose was abandoned). Lock held."""
        if self._traj_req is not None:
            self._traj_req.ok = False
            self._traj_req.done.set()
            self._traj_req = None
        self._segments.clear()
        self._segment_started = None

    # Streamed setpoints buffered beyond this are dropped from the front:
    # bounds added playback latency when delivery falls behind for a while.
    STREAM_QUEUE_CAP_S = 0.3

    def _on_arm_commands(self, msg: Float64MultiArray) -> None:
        values = list(msg.data)[: len(ARM_JOINTS)]
        now = time.monotonic()
        with self._lock:
            gap = now - self._last_stream_at
            new_stream = self._last_stream_at == 0.0 or gap > 0.5
            self._last_stream_at = now
            # Preempt goto trajectories and stale streams, but within a live
            # stream APPEND rather than clear: zenoh delivers the 50Hz stream
            # in ~100ms clumps under bulk camera traffic, and clearing per
            # message collapses each clump into one PD teleport.
            if self._traj_req is not None or new_stream:
                self._fail_active_traj()
            # Duration = stream cadence via an EMA over gaps (the mean of a
            # clump [~0,...,120ms] is the true rate), not the raw gap.
            if new_stream:
                self._stream_interval = 0.1  # bootstrap: first segment ramps gently
            else:
                self._stream_interval = 0.8 * self._stream_interval + 0.2 * gap
            interval = min(0.25, max(0.02, self._stream_interval))
            if self._segments:
                start = dict(self._segments[-1][1])
            else:
                current = self.sim.joint_targets()
                start = {k: current[k] for k in ARM_JOINTS}
            target = dict(start)
            for name, value in zip(ARM_JOINTS, values, strict=False):
                target[name] = float(value)
            self._segments.append((start, target, interval))
            while sum(d for _, _, d in self._segments) > self.STREAM_QUEUE_CAP_S:
                self._segments.pop(0)
                self._segment_started = None

    def _on_reset(self, _msg: Empty) -> None:
        with self._lock:
            self._fail_active_traj()
            self.sim.reset()

    def _on_head_position(self, msg: Int32) -> None:
        deg = max(HEAD_MIN_DEG, min(HEAD_MAX_DEG, float(msg.data)))
        with self._lock:
            self.sim.set_joint_target("joint_head", math.radians(deg))

    def _enqueue_segments(self, waypoints: list[dict], durations: list[float]) -> bool:
        req = SimpleNamespace(done=threading.Event(), ok=False)
        with self._lock:
            self._fail_active_traj()  # latest command wins, like the real arm
            current = self.sim.joint_targets()
            start = {k: current[k] for k in ARM_JOINTS}
            for target, duration in zip(waypoints, durations, strict=True):
                self._segments.append((start, target, max(duration, 0.05)))
                start = target
            self._traj_req = req
        total = sum(durations)
        return req.done.wait(timeout=total + 5.0) and req.ok

    def _on_goto_js(self, request, response):
        values = list(request.data.data)
        if len(values) < len(ARM_JOINTS):
            self.get_logger().warning(f"goto_js: got {len(values)} joint values, need {len(ARM_JOINTS)}; rejecting")
            response.success = False
            return response
        target = dict(zip(ARM_JOINTS, values, strict=False))
        response.success = self._enqueue_segments([target], [float(request.time)])
        return response

    def _on_goto_js_trajectory(self, request, response):
        n = int(request.num_joints)
        flat = list(request.waypoints.data)
        if n < len(ARM_JOINTS) or not flat or len(flat) % n != 0:
            self.get_logger().warning(
                f"goto_js_trajectory: invalid shape (num_joints={n}, {len(flat)} values); rejecting"
            )
            response.success = False
            return response
        waypoints = [dict(zip(ARM_JOINTS, flat[i : i + n], strict=False)) for i in range(0, len(flat), n)]
        durations = [float(d) for d in request.segment_durations]
        if len(durations) == len(waypoints) - 1:  # first segment: from current pose
            durations = [1.0, *durations]
        if len(durations) != len(waypoints):
            self.get_logger().warning(
                f"goto_js_trajectory: {len(waypoints)} waypoints but {len(durations)} durations; rejecting"
            )
            response.success = False
            return response
        response.success = self._enqueue_segments(waypoints, durations)
        return response

    def _on_trigger_noop(self, _request, response):
        response.success = True
        return response

    def _on_head_ai_position(self, _request, response):
        with self._lock:
            self.sim.set_joint_target("joint_head", math.radians(-20.0))
        response.success = True
        return response

    # --- trajectory servo (physics itself runs in the world server) ---

    def _trajectory_loop(self) -> None:
        """Interpolate queued segments at ~50Hz."""
        while rclpy.ok():
            try:
                with self._lock:
                    self._advance_trajectory()
            except Exception as exc:  # noqa: BLE001 -- this thread must never die
                self.get_logger().error(f"trajectory tick failed ({exc!r}); dropping active trajectory")
                with self._lock:
                    self._fail_active_traj()
            time.sleep(0.02)

    def _sim_now(self) -> float:
        # The server steps physics against the wall clock, so local monotonic
        # time IS the sim clock -- no RPC and no 15ms cache quantization.
        return time.monotonic()

    def _advance_trajectory(self) -> None:
        """Linear joint-space interpolation of the queued segments (lock
        held). Completed segments roll the clock forward within one tick, or
        a 50Hz stream would play back at a fraction of its speed."""
        if not self._segments:
            return
        now = self._sim_now()
        if self._segment_started is None:
            self._segment_started = now
        while self._segments and now - self._segment_started >= self._segments[0][2]:
            _start, end, duration = self._segments.pop(0)
            self._segment_started += duration
            if not self._segments:
                self.sim.set_joint_targets(dict(end))  # land exactly on the final pose
                self._segment_started = None
                if self._traj_req is not None:
                    self._traj_req.ok = True
                    self._traj_req.done.set()
                    self._traj_req = None
                return
        start, end, duration = self._segments[0]
        alpha = min(1.0, max(0.0, (now - self._segment_started) / duration))
        # One batched RPC per tick, not one per joint (7x fewer round-trips).
        self.sim.set_joint_targets({name: start[name] + alpha * (end[name] - start[name]) for name in ARM_JOINTS})

    # --- outputs ---

    def _stamp(self):
        return self.get_clock().now().to_msg()

    def _publish_odom(self) -> None:
        with self._lock:
            x, y, yaw = self.sim.pose()
        stamp = self._stamp()

        # Like bringup.py: pose only, zero twist/covariance.
        odom = Odometry()
        odom.header.stamp = stamp
        odom.header.frame_id = "odom"
        odom.child_frame_id = "base_link"
        odom.pose.pose.position.x = x
        odom.pose.pose.position.y = y
        odom.pose.pose.orientation.z = math.sin(yaw / 2)
        odom.pose.pose.orientation.w = math.cos(yaw / 2)
        self._odom_pub.publish(odom)

        tf = TransformStamped()
        tf.header.stamp = stamp
        tf.header.frame_id = "odom"
        tf.child_frame_id = "base_link"
        tf.transform.translation.x = x
        tf.transform.translation.y = y
        tf.transform.rotation.z = math.sin(yaw / 2)
        tf.transform.rotation.w = math.cos(yaw / 2)
        self._tf.sendTransform(tf)

    def _publish_scan(self) -> None:
        try:
            with self._lock:
                ranges = self.sim.lidar_scan(LIDAR_N_RAYS, LIDAR_RANGE_MAX)
        except (OSError, RuntimeError):
            return  # server briefly away; skip this scan
        msg = LaserScan()
        msg.header.stamp = self._stamp()
        msg.header.frame_id = "base_laser"
        msg.angle_min = -math.pi
        msg.angle_max = math.pi - 2 * math.pi / LIDAR_N_RAYS
        msg.angle_increment = 2 * math.pi / LIDAR_N_RAYS
        msg.scan_time = 1.0 / SCAN_HZ
        msg.time_increment = 0.0
        msg.range_min = LIDAR_RANGE_MIN
        msg.range_max = LIDAR_RANGE_MAX
        # core scans CCW from the robot's +x; LaserScan here starts at -pi.
        msg.ranges = np.roll(ranges, -LIDAR_N_RAYS // 2).astype(np.float32).tolist()
        self._scan_pub.publish(msg)

    def _render_loop(self) -> None:
        """Paced render pulls off the executor, with adaptive shedding: when
        demand oversubscribes the thread, camera periods stretch while depth
        keeps its rate -- nav stays healthy, cameras degrade gracefully."""
        last = {"main": 0.0, "wrist": 0.0, "depth": 0.0}
        period = {"main": 1.0 / MAIN_CAMERA_FPS, "wrist": 1.0 / WRIST_CAMERA_FPS, "depth": 1.0 / DEPTH_FPS}
        cost = {"main": 0.0, "wrist": 0.0, "depth": 0.0}  # EMA seconds per render
        target_util = 0.85  # leave headroom for physics/publishing on the other threads
        stretch = 1.0
        saturated = False

        while rclpy.ok():
            wanted = {
                "main": self._main_pub.get_subscription_count() > 0 or self._main_raw_pub.get_subscription_count() > 0,
                "wrist": self._wrist_pub.get_subscription_count() > 0
                or self._wrist_raw_pub.get_subscription_count() > 0,
                "depth": self._depth_pub.get_subscription_count() > 0 or self._points_pub.get_subscription_count() > 0,
            }
            demand = sum(cost[s] / period[s] for s in wanted if wanted[s])
            stretch = max(1.0, demand / target_util)
            if (stretch > 1.5) != saturated:
                saturated = stretch > 1.5
                if saturated:
                    self.get_logger().warning(
                        f"render thread saturated; reducing camera rates ~{stretch:.1f}x (depth keeps {DEPTH_FPS} Hz)"
                    )
                else:
                    self.get_logger().info("render thread recovered; camera rates back to target")

            now = time.monotonic()
            rendered = False
            if wanted["depth"] and now - last["depth"] >= period["depth"]:
                last["depth"] = now
                rendered = True
                t0 = time.perf_counter()
                try:
                    depth = self.sim.render_depth("main")
                except (OSError, RuntimeError) as exc:
                    self.get_logger().warning(f"depth render unavailable ({exc}); skipping frame")
                    continue
                cost["depth"] = 0.8 * cost["depth"] + 0.2 * (time.perf_counter() - t0)
                self._publish_depth_and_points(depth)
            for camera, pub, raw_pub in (
                ("main", self._main_pub, self._main_raw_pub),
                ("wrist", self._wrist_pub, self._wrist_raw_pub),
            ):
                if not wanted[camera] or now - last[camera] < period[camera] * stretch:
                    continue  # lazy, like the real drivers; stretched under load
                last[camera] = now
                rendered = True
                t0 = time.perf_counter()
                try:
                    jpeg = self.sim.render_jpeg(camera)
                except (OSError, RuntimeError) as exc:
                    self.get_logger().warning(f"{camera} render unavailable ({exc}); skipping frame")
                    continue
                cost[camera] = 0.8 * cost[camera] + 0.2 * (time.perf_counter() - t0)
                self._publish_camera_frames(pub, raw_pub, camera, jpeg)
            if not rendered:
                time.sleep(0.02)

    def _publish_camera_frames(self, pub, raw_pub, camera: str, jpeg: bytes) -> None:
        """Publish one camera frame; the server hands us a wire-res JPEG."""
        stamp = self._stamp()
        frame_id = "camera_optical_frame" if camera == "main" else "arm_camera_link"

        if pub.get_subscription_count() > 0:
            msg = CompressedImage()
            msg.header.stamp = stamp
            msg.header.frame_id = frame_id
            msg.format = "jpeg"
            msg.data = jpeg
            pub.publish(msg)

        if raw_pub.get_subscription_count() > 0:
            rgb = np.asarray(PILImage.open(io.BytesIO(jpeg)).convert("RGB"))
            msg = Image()
            msg.header.stamp = stamp
            msg.header.frame_id = frame_id
            msg.height, msg.width = rgb.shape[:2]
            msg.encoding = "bgr8"  # ROS camera-driver convention
            msg.is_bigendian = False
            msg.step = msg.width * 3
            msg.data = rgb[:, :, ::-1].tobytes()
            raw_pub.publish(msg)

    def _publish_depth_and_points(self, depth) -> None:
        """Any render scale in, fixed wire contract out: depth image 640x480,
        cloud ~160x120."""
        want_depth = self._depth_pub.get_subscription_count() > 0
        want_points = self._points_pub.get_subscription_count() > 0
        stamp = self._stamp()
        invalid = (depth < DEPTH_MIN_M) | (depth > DEPTH_MAX_M)
        img_scale = CAMERA_HEIGHT // depth.shape[0]

        if want_depth:
            # 16SC1 mm, invalid=0 (publishing.cpp convention); nearest-neighbor
            # upscale only -- depth must not interpolate across edges.
            mm = np.where(invalid, 0, depth * 1000.0)
            mm = np.clip(mm, 0, np.iinfo(np.int16).max).astype(np.int16)
            if img_scale > 1:
                mm = np.repeat(np.repeat(mm, img_scale, axis=0), img_scale, axis=1)
            msg = Image()
            msg.header.stamp = stamp
            msg.header.frame_id = "camera_optical_frame"
            msg.height, msg.width = mm.shape
            msg.encoding = "16SC1"
            msg.is_bigendian = False
            msg.step = msg.width * 2
            msg.data = mm.tobytes()
            self._depth_pub.publish(msg)

        if want_points:
            step = max(1, POINTS_SUBSAMPLE // img_scale)
            vs, us = _points_grid(depth.shape[0], depth.shape[1], step)
            f, cx, cy = FOCAL / img_scale, CX / img_scale, CY / img_scale
            z = np.where(invalid, np.nan, depth)[::step, ::step].astype(np.float32)
            x = (us - cx) / f * z
            y = (vs - cy) / f * z
            cloud = np.stack([x, y, z], axis=-1)

            msg = PointCloud2()
            msg.header.stamp = stamp
            msg.header.frame_id = "camera_optical_frame"
            msg.height, msg.width = cloud.shape[:2]
            msg.fields = [
                PointField(name=n, offset=4 * i, datatype=PointField.FLOAT32, count=1) for i, n in enumerate("xyz")
            ]
            msg.is_bigendian = False
            msg.point_step = 12
            msg.row_step = 12 * msg.width
            msg.is_dense = False
            msg.data = cloud.tobytes()
            self._points_pub.publish(msg)

    def _publish_camera_info(self) -> None:
        msg = CameraInfo()
        msg.header.stamp = self._stamp()
        msg.header.frame_id = "camera_optical_frame"
        msg.width, msg.height = CAMERA_WIDTH, CAMERA_HEIGHT
        msg.distortion_model = "plumb_bob"
        msg.d = [0.0] * 5
        msg.k = [FOCAL, 0.0, CX, 0.0, FOCAL, CY, 0.0, 0.0, 1.0]
        msg.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        msg.p = [FOCAL, 0.0, CX, 0.0, 0.0, FOCAL, CY, 0.0, 0.0, 0.0, 1.0, 0.0]
        self._caminfo_pub.publish(msg)

    def _publish_joint_states(self) -> None:
        with self._lock:
            positions = self.sim.joint_positions()
        msg = JointState()
        msg.header.stamp = self._stamp()
        msg.name = [*ARM_JOINTS, "joint_head"]  # same 7 as the real arm node
        msg.position = [positions[n] for n in msg.name]
        self._joint_states_pub.publish(msg)

    def _publish_arm_state(self) -> None:
        with self._lock:
            positions = self.sim.joint_positions()
        msg = JointState()
        msg.header.stamp = self._stamp()
        msg.name = list(ARM_JOINTS)
        msg.position = [positions[n] for n in ARM_JOINTS]
        self._arm_state_pub.publish(msg)

    def _publish_active_streams(self) -> None:
        msg = String()
        msg.data = json.dumps({"cameras": ["main", "arm", "orbit"], "count": 0, "clients": [], "simulated": True})
        self._streams_pub.publish(msg)

    def _publish_head(self) -> None:
        with self._lock:
            pitch = self.sim.head_pitch_deg()
        msg = String()
        msg.data = json.dumps(
            {
                "current_position": pitch,
                "min_angle": HEAD_MIN_DEG,
                "max_angle": HEAD_MAX_DEG,
                "default_angle": 0.0,
            }
        )
        self._head_pub.publish(msg)


def main() -> None:
    rclpy.init()
    node = VirtualMarsNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        with contextlib.suppress(Exception):  # SIGINT may have shut rcl down already
            rclpy.shutdown()


if __name__ == "__main__":
    main()
