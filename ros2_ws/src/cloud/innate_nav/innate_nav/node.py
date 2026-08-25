#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""ROS 2 node for the Innate navigation policy (innate-nav-inference).

Streams the wide camera view to the policy server, tracks the plans it sends back,
and drives the base. Structurally the sibling of innate_uninavid: an action
server the skill calls, a camera subscription feeding a websocket, and a
cmd_vel publisher -- the difference is what comes back. UniNavid returns
discrete actions; this returns 8 metric waypoints per plan, which the robot
tracks continuously between plans.

Three independent clocks (see innate-nav-inference README):
  streaming  -- every camera frame goes up as it arrives (~5Hz), fire and forget
  inference  -- the server decides when to plan; we never poll or wait
  control    -- this node's 50Hz timer tracks the newest plan continuously

The waypoints in a plan are egocentric AT THEIR CAPTURE POSE and arrive a few
hundred ms old, so every control tick re-expresses them through the odometry
delta since that pose. Skipping that puts a systematic 0.1-0.3m bias into the
tracked path.

Manual test::

    ros2 action send_goal /innate_nav/navigate \
        innate_cloud_msgs/action/NavigateInstruction \
        '{instruction: "drive into the dining area", server: "10.0.0.4"}' --feedback

An empty `server` uses the node's configured one.
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import deque

import cv2
import rclpy
from geometry_msgs.msg import PoseStamped, Twist
from innate_cloud_msgs.action import NavigateInstruction
from innate_cloud_msgs.srv import CheckPolicyServer
from nav_msgs.msg import OccupancyGrid, Odometry, Path
from rclpy.action import ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CameraInfo, CompressedImage
from std_msgs.msg import String

from .costmap import Costmap
from .introspect import ObservationStrip
from .policy_client import PolicyClient, Pose
from .pursuit import (
    BlockedDetector,
    PursuitCfg,
    avoid,
    odom_path_from,
    plan_waypoints,
    rate_limit,
    reanchor,
    recovery_turn,
    speed_scale,
    steering_target,
    to_odom,
    truncate_to,
    velocity,
)
from .utils import server_url

_STOP = Twist()
CONTROL_HZ = 50.0
# How long a blocked recovery spins before handing back to the policy. The
# direction is latched for the whole window: re-deciding per tick makes the
# robot dither, because each new plan flips it.
RECOVERY_S = 1.5
# How long a goal waits for its first plan before giving up. Long enough for a
# cold server and a slow link, short enough that a misconfigured robot says so
# instead of sitting still until someone reloads the page.
FIRST_PLAN_S = 20.0
# Operator status rate. Fast enough that "a new observation went up" reads as
# live, slow enough to stay off the control thread's back.
STATUS_HZ = 5.0
# The strip carries the window at the model's own resolution -- a quarter of a
# megabyte an update -- so it goes out slower than plans arrive. The window
# barely changes between consecutive plans anyway.
STRIP_HZ = 1.0
# A connection test answers quickly or it is not a test -- someone is watching
# a button, and "no route to host" should not take the run-start timeout.
PROBE_TIMEOUT_S = 4.0


def _fx_of(camera: dict) -> float:
    """The focal length implied by a camera spec: fx = (w/2) / tan(hfov/2)."""
    w, hfov = float(camera.get("width", 0)), float(camera.get("hfov", 0))
    if w <= 0 or not 0 < hfov < 180:
        return 0.0
    return (w / 2) / math.tan(math.radians(hfov) / 2)


def _yaw_of(odom: Odometry) -> float:
    q = odom.pose.pose.orientation
    return 2.0 * math.atan2(q.z, q.w)


class InnateNavNode(Node):
    def __init__(self) -> None:
        super().__init__("innate_nav_node")

        self.declare_parameter(
            "server_url", os.getenv("INNATE_NAV_SERVER_URL", "http://192.168.0.156:8900")
        )
        # The policy runs on a server, not on the robot; today that is an IP on
        # the LAN and later it will be a proxy hostname. Either way the robot
        # holds a bearer token, and the URL scheme decides ws vs wss on its own.
        self.declare_parameter("server_token", os.getenv("INNATE_NAV_SERVER_TOKEN", ""))
        self.declare_parameter("camera_topic", "/mars/main_camera/left/wide/image_rect/compressed")
        self.declare_parameter("task_family", "r2r")
        self.declare_parameter("token_budget", 3072)
        self.declare_parameter("max_linear_speed", PursuitCfg().v_max)
        self.declare_parameter("max_angular_speed", PursuitCfg().w_max)
        # The policy publishes straight to /cmd_vel_nav, which is where Nav2's
        # velocity_smoother writes -- so nothing downstream limits how fast
        # these commands change. This is that limit.
        self.declare_parameter("max_linear_accel", PursuitCfg().a_lin)
        self.declare_parameter("max_linear_decel", PursuitCfg().d_lin)
        self.declare_parameter("max_angular_accel", PursuitCfg().a_ang)
        self.declare_parameter("max_angular_decel", PursuitCfg().d_ang)
        self.declare_parameter("publish_path", True)
        self.declare_parameter("use_costmap", True)
        self.declare_parameter("camera_info_topic", "/mars/main_camera/left/wide/camera_info")
        # The focal length the checkpoint was trained at (110 deg over 640px).
        self.declare_parameter("expected_fx", 224.07)

        self._server_url = str(self.get_parameter("server_url").value)
        self._server_token = str(self.get_parameter("server_token").value) or None
        self._cfg = PursuitCfg(
            v_max=float(self.get_parameter("max_linear_speed").value),
            w_max=float(self.get_parameter("max_angular_speed").value),
            a_lin=float(self.get_parameter("max_linear_accel").value),
            d_lin=float(self.get_parameter("max_linear_decel").value),
            a_ang=float(self.get_parameter("max_angular_accel").value),
            d_ang=float(self.get_parameter("max_angular_decel").value),
        )

        self._client: PolicyClient | None = None
        self._costmap: Costmap | None = None
        self._warned_no_costmap = False
        self._checked_intrinsics = False
        self._cam_fx = 0.0
        self._pose: Pose | None = None
        self._goal_handle = None
        self._cancel_requested = threading.Event()
        self._arrived = threading.Event()
        self._blocked = BlockedDetector()
        self._recovering_until = 0.0
        self._recovery_w = 0.0
        self._last_seq = 0
        self._cmd_v = 0.0
        self._cmd_w = 0.0
        self._cmd_at = 0.0
        self._frames_seen = 0
        self._frames_sent = 0
        self._last_sent_at = 0.0
        self._instruction = ""
        self._episode_id = ""
        self._server = ""
        self._t0 = time.monotonic()

        sensor_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT, history=HistoryPolicy.KEEP_LAST, depth=1
        )
        self.create_subscription(
            CompressedImage,
            str(self.get_parameter("camera_topic").value),
            self._on_image,
            sensor_qos,
            callback_group=MutuallyExclusiveCallbackGroup(),
        )
        self.create_subscription(
            Odometry, "/odom", self._on_odom, 1, callback_group=MutuallyExclusiveCallbackGroup()
        )
        self.create_subscription(
            CameraInfo,
            str(self.get_parameter("camera_info_topic").value),
            self._on_camera_info,
            10,
            callback_group=MutuallyExclusiveCallbackGroup(),
        )
        # Nav2's local costmap: /scan inflated against the live footprint (the
        # depth-cloud STVL layer is disabled, so this is the lidar plane only).
        # Latched (TRANSIENT_LOCAL): it republishes on change, not on a timer.
        if bool(self.get_parameter("use_costmap").value):
            self.create_subscription(
                OccupancyGrid,
                "/local_costmap/costmap",
                self._on_costmap,
                QoSProfile(
                    reliability=ReliabilityPolicy.RELIABLE,
                    durability=DurabilityPolicy.TRANSIENT_LOCAL,
                    history=HistoryPolicy.KEEP_LAST,
                    depth=1,
                ),
                callback_group=MutuallyExclusiveCallbackGroup(),
            )
        # Below teleop and skills in cmd_vel_mux, so a human at the joystick
        # always wins and a dead node stops the base within the mux's window.
        self._cmd = self.create_publisher(Twist, "/cmd_vel_nav", 10)
        self._path = self.create_publisher(Path, "/nav_policy/path", 1)
        # Operator view. Both are lazy: the strip costs a decode + resize per
        # frame, which an unwatched robot should not pay.
        self._status_pub = self.create_publisher(String, "/nav_policy/status", 1)
        self._obs_pub = self.create_publisher(
            CompressedImage, "/nav_policy/observations/compressed", 1)
        self._obs = ObservationStrip()
        self._obs_at = 0.0
        self._plan_times: deque[float] = deque(maxlen=16)
        self.create_timer(1.0 / STATUS_HZ, self._publish_status,
                          callback_group=MutuallyExclusiveCallbackGroup())
        self.create_timer(
            1.0 / CONTROL_HZ, self._control_tick, callback_group=MutuallyExclusiveCallbackGroup()
        )

        self._action_server = ActionServer(
            self,
            NavigateInstruction,
            "/innate_nav/navigate",
            execute_callback=self._execute,
            goal_callback=lambda _: GoalResponse.ACCEPT,
            handle_accepted_callback=self._on_accepted,
            cancel_callback=lambda _: CancelResponse.ACCEPT,
            callback_group=ReentrantCallbackGroup(),
        )
        # Its own group: the probe blocks on the network for a few seconds and
        # must not hold up the control tick while it does.
        self._check_srv = self.create_service(
            CheckPolicyServer, "/nav_policy/check_server", self._check_server,
            callback_group=MutuallyExclusiveCallbackGroup())
        self.get_logger().info(f"InnateNavNode ready (policy server {self._server_url})")

    # ── sensing ───────────────────────────────────────────────────────────

    def _on_odom(self, msg: Odometry) -> None:
        p = msg.pose.pose.position
        self._pose = Pose(p.x, p.y, _yaw_of(msg))

    def _on_costmap(self, msg: OccupancyGrid) -> None:
        first = self._costmap is None
        self._costmap = Costmap.from_msg(msg)
        if first:
            cm = self._costmap
            self.get_logger().info(
                f"local costmap up: {cm.grid.shape[1]}x{cm.grid.shape[0]} @ "
                f"{cm.resolution:.3f}m — clearance checks active")

    def _on_camera_info(self, msg: CameraInfo) -> None:
        """The geometry is the contract, so check it rather than trusting the
        topic name. A camera whose focal length is not the one the policy was
        trained at makes it misjudge every distance, silently and plausibly."""
        self._cam_fx = float(msg.k[0])
        if self._checked_intrinsics:
            return
        self._checked_intrinsics = True
        want = float(self.get_parameter("expected_fx").value)
        fx, fy = msg.k[0], msg.k[4]
        if abs(fx - want) > 1.0 or abs(fy - want) > 1.0:
            self.get_logger().error(
                f"camera fx={fx:.1f} fy={fy:.1f} but the checkpoint was trained at "
                f"{want:.1f} -- the policy will read distances off by {want / max(fx, 1e-6):.2f}x. "
                f"Wrong camera, or the rectifier's hfov_deg does not match training."
            )
        else:
            self.get_logger().info(f"camera intrinsics match training (fx={fx:.2f})")

    def _on_image(self, msg: CompressedImage) -> None:
        """Every frame goes up as-is. The server owns keyframe selection, so
        this must not thin the stream on its own -- it would be deciding a
        training-distribution question it cannot see."""
        self._frames_seen += 1
        client, pose = self._client, self._pose
        if client is None or pose is None:
            return
        stamp = time.monotonic() - self._t0
        try:
            client.send_frame(bytes(msg.data), pose, stamp)
            self._frames_sent += 1
            self._last_sent_at = stamp
            self._obs.remember(bytes(msg.data), stamp)
        except Exception as exc:  # noqa: BLE001 -- a dead socket ends the goal, not the node
            self.get_logger().warning(f"frame upload failed ({exc!r}); ending goal")
            self._cancel_requested.set()

    # ── control ───────────────────────────────────────────────────────────

    def _control_tick(self) -> None:
        client, pose = self._client, self._pose
        if client is None or pose is None:
            return
        plan = client.plan
        if plan is None:
            return

        if plan.stop:
            self._halt()
            self._arrived.set()
            return

        wp = reanchor(plan_waypoints(plan), plan.capture_pose, pose)
        v, w = self._track(wp, pose)

        now = time.monotonic()
        if now < self._recovering_until:
            v, w = 0.0, self._recovery_w
        elif self._blocked.update(now, self._cmd_v, pose):
            # The camera cannot see "wedged against a wall" -- pressed into one
            # the view is a texture close-up, far off-distribution, and the
            # policy keeps confidently predicting forward. Odometry disagreeing
            # with the command is the only honest signal -- and the command is
            # the ramped one that reached the wheels, never the tracker's
            # request, which during every ramp-up is a speed nothing was asked
            # to travel at.
            self._recovery_w = recovery_turn(wp, self._cfg)
            self._recovering_until = now + RECOVERY_S
            self._blocked.reset()
            self.get_logger().info(
                f"blocked at ({pose.x:.2f}, {pose.y:.2f}); turning "
                f"{'left' if self._recovery_w > 0 else 'right'}"
            )
            v, w = 0.0, self._recovery_w

        self._drive(v, w)

        # One counter for "this plan is new", reset with the episode: a second
        # one carried across episodes and silently swallowed every operator
        # view until a run outlived the last one's plan count.
        if plan.seq > self._last_seq:
            self._last_seq = plan.seq
            self._plan_times.append(time.monotonic())
            self._publish_observations(plan)
            if bool(self.get_parameter("publish_path").value):
                self._publish_path(wp, pose)

    def _drive(self, v: float, w: float) -> None:
        """Every moving command goes out through here, ramped.

        The tracker recomputes v and w from scratch each tick, so a new plan or
        a recovery turn can ask for a step change the base answers with a lurch
        -- and on a robot carrying an arm, a lurch is what puts the arm into a
        doorframe. Measured dt rather than the nominal tick: a late tick has
        genuinely had longer to accelerate, and pretending otherwise makes the
        ramp lie about the speed the base is already at.
        """
        now = time.monotonic()
        dt = min(now - self._cmd_at, 4.0 / CONTROL_HZ) if self._cmd_at else 1.0 / CONTROL_HZ
        self._cmd_at = now
        self._cmd_v = rate_limit(self._cmd_v, v, self._cfg.a_lin, self._cfg.d_lin, dt)
        self._cmd_w = rate_limit(self._cmd_w, w, self._cfg.a_ang, self._cfg.d_ang, dt)
        twist = Twist()
        twist.linear.x, twist.angular.z = self._cmd_v, self._cmd_w
        self._cmd.publish(twist)

    def _halt(self) -> None:
        """Stop now, unramped: a cancel, an abort or an arrival must not coast.
        The ramp restarts from rest so the next run does not inherit a stale
        speed the base is no longer moving at."""
        self._cmd_v = self._cmd_w = 0.0
        self._cmd_at = 0.0
        self._cmd.publish(_STOP)

    def _track(self, wp_m, pose: Pose) -> tuple[float, float]:
        """Pure pursuit, clipped and nudged by the local costmap.

        The policy plans for the 0.1 m-radius agent it was trained on, so its
        line shaves corners this body cannot take. Three corrections, cheapest
        first: cut the path where the robot's centre may no longer go, push the
        steering target off nearby obstacles, and scale speed by how close the
        remaining path runs. Without a costmap it degrades to plain pursuit.
        """
        costmap = self._costmap
        if costmap is None:
            # Say so out loud, once: a safety layer that is silently absent is
            # worse than one that was never enabled, because the robot drives
            # exactly as before and nothing reports why.
            if bool(self.get_parameter("use_costmap").value) and not self._warned_no_costmap:
                self._warned_no_costmap = True
                self.get_logger().warning(
                    "no /local_costmap/costmap yet — driving WITHOUT clearance "
                    "checks (is Nav2's local costmap up?)")
            return velocity(wp_m, self._cfg)

        safe = costmap.safe_length(odom_path_from(wp_m, pose))
        kept = truncate_to(wp_m, safe)
        if len(kept) < 1:
            # Nowhere ahead is drivable: turn toward the policy's freest bearing
            # rather than creep into whatever is in the way.
            return 0.0, recovery_turn(wp_m, self._cfg)

        target = steering_target(kept, self._cfg.lookahead_m)
        rep_odom = costmap.repulsion(*to_odom(target.reshape(1, 3), pose)[0])
        if rep_odom.any():
            c, s = math.cos(pose.yaw), math.sin(pose.yaw)
            target = avoid(target, (c * rep_odom[0] + s * rep_odom[1],
                                    -s * rep_odom[0] + c * rep_odom[1]), self._cfg)

        bearing = math.atan2(target[1], target[0])
        w = max(-self._cfg.w_max, min(self._cfg.w_max, self._cfg.k_yaw * bearing))
        if abs(math.degrees(bearing)) > self._cfg.turn_threshold_deg:
            return 0.0, w
        # Proximity over the NEAR stretch only. Judging it over the whole kept
        # path lets an obstacle 2 m away brake the robot now -- and worse, the
        # kept path ends exactly on the cell that stopped it, so the worst cost
        # was pinned at INSCRIBED and the robot never moved at all.
        near = truncate_to(kept, self._cfg.lookahead_m)
        scale = speed_scale(costmap.worst_cost(odom_path_from(near, pose)), self._cfg)
        return self._cfg.v_max * max(0.0, math.cos(bearing)) * scale, w

    def _publish_observations(self, plan) -> None:
        """The window this plan was made from, as the strip of frames the model
        saw, each at the resolution it was given. Only the robot still has the
        frames; the server names them by the stamp the robot assigned, and says
        what size it resized each to."""
        now = time.monotonic()
        if now - self._obs_at < 1.0 / STRIP_HZ:
            return
        strip = self._obs.strip(list(plan.history_stamps), list(plan.history_sizes))
        if strip is None:
            return
        self._obs_at = now
        msg = CompressedImage()
        msg.header.stamp = self.get_clock().now().to_msg()
        # Not a TF frame: the plan this strip was composed from. Status and
        # strip travel on separate topics at separate rates, so a viewer that
        # pairs them by arrival draws one plan's window over another's frames.
        msg.header.frame_id = str(plan.seq)
        msg.format = "jpeg"
        msg.data = cv2.imencode(".jpg", strip, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()
        self._obs_pub.publish(msg)

    def _plan_rate_hz(self) -> float:
        if len(self._plan_times) < 2:
            return 0.0
        span = self._plan_times[-1] - self._plan_times[0]
        return (len(self._plan_times) - 1) / span if span > 0 else 0.0

    def _check_server(self, request: CheckPolicyServer.Request,
                      response: CheckPolicyServer.Response) -> CheckPolicyServer.Response:
        """Can this robot reach that policy server, and is it the right one?

        Reachability is only half the question. A server that answers happily
        while serving a checkpoint trained at a different focal length drives
        the robot into things for reasons no log line explains, so the camera
        the robot actually has is checked against the one the checkpoint was
        trained on before anyone starts a run.
        """
        server = server_url(request.server, self._server_url)
        try:
            info = PolicyClient(server, token=self._server_token,
                                timeout_s=PROBE_TIMEOUT_S).model_info()
        except Exception as exc:  # noqa: BLE001 -- every failure is an answer here
            response.success = False
            response.message = f"{server} unreachable: {exc}"
            return response

        cam = info.get("camera") or {}
        parts = [f"{info.get('model', '?')} on {info.get('device', '?')}",
                 f"history {info.get('history_k', '?')}",
                 f"budgets {info.get('token_budgets', [])}"]
        want = _fx_of(cam)
        if want and self._cam_fx:
            off = self._cam_fx / want
            if abs(off - 1.0) > 0.005:
                response.success = False
                response.message = (
                    f"reachable ({parts[0]}) but WRONG CAMERA: this robot publishes "
                    f"fx={self._cam_fx:.1f} and the checkpoint was trained at {want:.1f}, "
                    f"so it would read every distance {want / self._cam_fx:.2f}x off")
                return response
            parts.append(f"camera matches (fx {want:.1f})")
        elif want:
            parts.append(f"camera fx {want:.1f} — nothing on {self.get_parameter('camera_info_topic').value} yet")

        response.success = True
        response.message = f"{server}: " + ", ".join(parts)
        return response

    def _publish_status(self) -> None:
        """One JSON snapshot for the operator view.

        Not gated on subscriber count: an operator view that silently stops
        because a count read 0 is worse than the few hundred bytes it costs.
        """
        client = self._client
        plan = client.plan if client is not None else None
        now = time.monotonic()
        state = {
            "running": client is not None,
            "instruction": self._instruction,
            "episode_id": self._episode_id,
            "server": self._server,
            "frames_seen": self._frames_seen,
            "frames_sent": self._frames_sent,
            # How long ago the newest observation went up: this is the
            # streaming clock, and it is independent of the plan rate.
            "last_frame_age_s": max(0.0, now - self._t0 - self._last_sent_at),
            "plan_rate_hz": self._plan_rate_hz(),
            "costmap": self._costmap is not None,
            "blocked_recovery": now < self._recovering_until,
            "arrived": self._arrived.is_set(),
        }
        if plan is not None:
            state["plan"] = {
                "seq": plan.seq,
                "age_s": max(0.0, now - self._t0 - plan.capture_stamp),
                "compute_ms": plan.compute_ms,
                "max_reach_m": plan.max_reach_m,
                "stop": plan.stop,
                "p_stop": plan.p_stop,
                "keyframes": plan.keyframes,
                "model": plan.model,
                "history_indices": list(plan.history_indices),
                # Age of each observation in the window, oldest first: the
                # spread is what shows uniform-vs-latest at a glance.
                "history_ages_s": [max(0.0, now - self._t0 - t) for t in plan.history_stamps],
                # Pixels the model was given per frame: the budget buys detail
                # at the recent end, and these are what say how much.
                "history_sizes": [list(s) for s in plan.history_sizes],
            }
        msg = String()
        msg.data = json.dumps(state)
        self._status_pub.publish(msg)

    def _clear_path(self) -> None:
        """Retire the overlay when the run ends. The topic only ever carries new
        plans, so the last one drawn otherwise stays on the floor of the 3D view
        until some later run happens to replace it."""
        if not bool(self.get_parameter("publish_path").value):
            return
        msg = Path()
        msg.header.frame_id = "odom"
        msg.header.stamp = self.get_clock().now().to_msg()
        self._path.publish(msg)

    def _publish_path(self, wp_m, at: Pose) -> None:
        """The plan as an odom-frame floor path, for the webapp's 3D view."""
        msg = Path()
        msg.header.frame_id = "odom"
        msg.header.stamp = self.get_clock().now().to_msg()
        cos_yaw, sin_yaw = math.cos(at.yaw), math.sin(at.yaw)
        for x, y, _theta in wp_m:
            ps = PoseStamped()
            ps.header = msg.header
            ps.pose.position.x = at.x + cos_yaw * x - sin_yaw * y
            ps.pose.position.y = at.y + sin_yaw * x + cos_yaw * y
            ps.pose.orientation.w = 1.0
            msg.poses.append(ps)
        self._path.publish(msg)

    def _stall_reason(self, server: str) -> str:
        """Why no plan arrived. Each branch is a different thing to go and fix,
        and without them the goal just sits: frames are dropped silently when
        there is no pose, so a robot missing /odom looks identical to a dead
        server."""
        topic = str(self.get_parameter("camera_topic").value)
        if self._frames_seen == 0:
            return f"no camera frames on {topic} in {FIRST_PLAN_S:.0f}s -- is the nav camera publishing?"
        if self._pose is None:
            return f"no /odom in {FIRST_PLAN_S:.0f}s -- frames carry a pose, so none could be sent"
        if self._frames_sent == 0:
            return f"{self._frames_seen} camera frames seen but none sent to {server}"
        return (
            f"sent {self._frames_sent} frames to {server} but no plan came back in "
            f"{FIRST_PLAN_S:.0f}s -- the server accepted the episode but is not planning"
        )

    # ── goal lifecycle ────────────────────────────────────────────────────

    def _on_accepted(self, goal_handle) -> None:
        old = self._goal_handle
        if old is not None and old.is_active:
            self.get_logger().info("preempting previous goal")
            old.abort()
        self._goal_handle = goal_handle
        goal_handle.execute()

    def _execute(self, goal_handle):
        instruction = goal_handle.request.instruction
        server = server_url(goal_handle.request.server, self._server_url)
        self.get_logger().info(f"executing: {instruction!r} via {server}")
        result = NavigateInstruction.Result()
        feedback = NavigateInstruction.Feedback()

        self._cancel_requested.clear()
        self._arrived.clear()
        self._blocked.reset()
        self._recovering_until = 0.0
        self._last_seq = 0
        self._cmd_v = 0.0
        self._cmd_w = 0.0
        self._cmd_at = 0.0
        self._frames_seen = 0
        self._frames_sent = 0

        client = PolicyClient(server, token=self._server_token)
        try:
            handle = client.start(
                instruction=instruction,
                task_family=str(self.get_parameter("task_family").value),
                token_budget=int(self.get_parameter("token_budget").value),
            )
        except Exception as exc:  # noqa: BLE001 -- server down is a goal failure
            self._halt()
            goal_handle.abort()
            return self._result(result, False, f"no policy server at {server}: {exc}")

        opened = time.monotonic()
        self._instruction, self._episode_id, self._server = instruction, handle.episode_id, server
        self._obs.clear()
        self._obs_at = 0.0
        self._plan_times.clear()
        self.get_logger().info(
            f"episode {handle.episode_id} [{handle.task_family}/{handle.history_mode}"
            f"/{handle.token_budget}]"
        )
        self._client = client
        try:
            while rclpy.ok() and goal_handle.is_active:
                if goal_handle.is_cancel_requested or self._cancel_requested.is_set():
                    self._halt()
                    goal_handle.canceled()
                    return self._result(result, False, "canceled")
                if self._arrived.is_set():
                    self._halt()
                    goal_handle.succeed()
                    return self._result(result, True, "arrived (waypoint collapse)")

                plan = client.plan
                if plan is None and time.monotonic() - opened > FIRST_PLAN_S:
                    self._halt()
                    goal_handle.abort()
                    return self._result(result, False, self._stall_reason(server))
                if plan is not None:
                    feedback.latest_action = 0 if plan.stop else 1
                    feedback.consecutive_stops = int(plan.stop)
                    feedback.max_consecutive_stops = 1
                    goal_handle.publish_feedback(feedback)
                time.sleep(0.1)

            self._halt()
            return self._result(result, False, "preempted")
        finally:
            # Only tear down if this goal still owns the client. A preempting
            # goal has already installed its own by the time the old one's loop
            # notices it went inactive, and clearing unconditionally would strip
            # the NEW episode of its client -- frames stop uploading, plans stop
            # being tracked, and the robot sits still until the goal times out.
            if self._client is client:
                self._client = None
                self._halt()
                self._clear_path()
            client.close()

    @staticmethod
    def _result(result, success: bool, message: str):
        result.success = success
        result.message = message
        return result

    def cancel(self) -> None:
        self._cancel_requested.set()


def main(args=None) -> None:
    rclpy.init(args=args)
    node = InnateNavNode()
    executor = MultiThreadedExecutor(num_threads=4)
    try:
        rclpy.spin(node, executor=executor)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
