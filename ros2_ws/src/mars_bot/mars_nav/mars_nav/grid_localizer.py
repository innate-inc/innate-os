#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Global lidar localization for the initial pose: finds the robot anywhere on the map.
A likelihood-field scan matcher (mars_nav/scan_match.py, CPU) searches the whole map.

Architecture:
┌─────────────────────┐     ┌─────────────────────┐
│   Grid Localizer    │────▶│        AMCL         │
│  (coarse estimate)  │     │  (fine refinement)  │
└─────────────────────┘     └─────────────────────┘
         │                           │
         │ /initialpose              │ continuous
         │ (transient_local QoS)     │ tracking
         ▼                           ▼
    Seeds AMCL's              Publishes map→odom
    particle filter           transform

Key features:
- Subscribes to /map and /scan_fast
- Scores poses over the whole map by how close the scan's endpoints land to walls,
  then refines the best distinct candidates to about a centimetre and a degree
- Always publishes the best pose to /initialpose with transient_local QoS (latched)
  and seeds AMCL with it; the status says whether it is trustworthy
- Watches AMCL's pose against the scan and relocalizes when AMCL has lost the robot,
  but only to a confident match that clearly fits better (`auto_recover`)
- Catches stalls, wheels spinning while the lidar shows the robot standing still: stops
  navigation and pulls AMCL back to where the scan puts the robot (`auto_recover`)
- Runs as a lifecycle node for proper initialization coordination

On startup, automatically localizes once a map and a scan are in.
Publishes status to /localization/status: 'localized' when the best pose holds at
least `confidence_threshold` of the evidence (other places that fit the scan nearly
as well take the rest), else 'localized_low_confidence'.
Service remains available for manual triggers after auto-localize completes.
"""

import math
import time
from collections import deque

import numpy as np
import rclpy
from builtin_interfaces.msg import Time
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav2_msgs.srv import SetInitialPose
from nav_msgs.msg import OccupancyGrid, Odometry
from rclpy.executors import ExternalShutdownException
from rclpy.lifecycle import Node, Publisher, State, TransitionCallbackReturn
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from rclpy.subscription import Subscription
from rclpy.task import Future
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String
from std_srvs.srv import Trigger

from mars_nav.localize_reply import LOW_CONFIDENCE, describe_pose
from mars_nav.scan_match import MERGE_DEG, MERGE_M, MIN_FIT, Estimate, Grid, Pose2D, Scan, evaluate, locate, refine

MAX_SCAN_AGE_S = 1.0  # /scan_fast runs at ~10 Hz: anything older means the lidar stopped
MIN_SCAN_POINTS = 10

WATCH_PERIOD_S = 1.0
LOST_STRIKES = 3  # consecutive watch ticks AMCL's pose must fail before the map-wide search
SETTLE_S = 5.0  # a new seed or map gets this long to take before AMCL's pose is judged
RETRY_S = 30.0  # a search that found nowhere clearly better is not repeated sooner
RECOVERY_MARGIN = 0.15  # a jump must put this much more of the scan on walls than AMCL's own pose
SCAN_HISTORY = 32  # ~4 s of /scan_fast: the scan AMCL's latest estimate came from, and the stall lookback
SAME_SCAN_S = 0.02  # AMCL's /scan is every other /scan_fast message, same stamps

STALL_LOOKBACK_S = 3.0  # how far back a stall check looks for a scan the wheels claim to have left
STALL_MIN_MOVE_M = 0.08  # wheel motion smaller than this, and than STALL_MIN_TURN, the lidar cannot judge
STALL_MIN_TURN = math.radians(8)
STALL_RANGE_M = 4.0  # nearby structure shows motion; far returns only slow the check
STALL_GRID_M = 0.02
STALL_INLIER_M = 0.03  # tight enough to tell a few centimetres of motion from none
STALL_MARGIN = 0.25  # "stood still" must fit this much more of the scan than the wheels' claimed motion
STALL_STRIKES = 3  # consecutive scans
STALL_QUIET_S = 3.0  # the base needs this long to stop before a stall can be judged again
STALL_SETTLE_S = 1.0  # AMCL is judged once the wheels have been still this long
STALL_FIX_TIMEOUT_S = 8.0  # a cancelled goal can take mode_manager up to 5 s to end; past this the wheels never stopped
STALL_SEARCH_M = 0.35  # at full speed AMCL absorbs up to ~0.3 m / 20° of phantom motion before a stall is caught
STALL_SEARCH_DEG = 25.0
STALL_DRIFT_M = 0.05  # an AMCL pose this close to the scan's after a stall is left alone
STALL_DRIFT_TURN = math.radians(3)
ODOM_HISTORY = 120  # ~4 s of /odom at 30 Hz
ODOM_MATCH_S = 0.1


class GridLocalizer(Node):
    """Global lidar localization lifecycle node."""

    # Subscriptions and publishers (created in on_configure)
    scan_sub: Subscription | None = None
    map_sub: Subscription | None = None
    pose_pub: Publisher | None = None
    status_pub: Publisher | None = None
    amcl_sub: Subscription | None = None
    odom_sub: Subscription | None = None
    srv = None
    hand_placed_srv = None
    _auto_timer = None
    _map_check_timer = None
    _watch_timer = None
    _stall_fix_timer = None

    # Map state
    map_received: bool = False
    grid: Grid | None = None
    map_received_time = None

    # Scan storage
    latest_scan: LaserScan | None = None
    _scan_received_at = 0.0

    # Auto-localize state
    _auto_done: bool = False
    _auto_localize_enabled: bool = False

    # Lost-AMCL watch state
    _auto_recover: bool = False
    _scans: deque[LaserScan]
    _amcl: tuple[float, Pose2D] | None = None  # (scan stamp, AMCL's pose at that scan)
    _lost_strikes: int = 0
    _watch_quiet_until: float = 0.0
    _verdict: str = ""  # the latest 'localized' / 'localized_low_confidence' published
    _watch_doubted: bool = False  # the watch downgraded a 'localized' verdict; restored once AMCL's pose fits again
    _reaffirm_after: float = 0.0  # a reaffirm search that failed is not repeated sooner; the watch itself goes on

    # Stall watch state
    _odom: deque[tuple[float, Pose2D]]  # (stamp, wheel-odometry pose)
    _odom_received_at: float = 0.0
    _stall_strikes: int = 0
    _stall_quiet_until: float = 0.0
    _stall_fix_deadline: float = 0.0

    # Node state tracking
    _is_active: bool = False

    # Parameters (declared in on_configure)
    max_range: float
    auto_timeout = None
    confidence_threshold: float

    def __init__(self, node_name="grid_localizer", **kwargs):
        super().__init__(node_name, **kwargs)
        self._scans = deque(maxlen=SCAN_HISTORY)
        self._odom = deque(maxlen=ODOM_HISTORY)
        self._cancel_nav_client = self.create_client(Trigger, "/nav/cancel_navigation")

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Unconfigured → Inactive: Declare parameters and create resources."""
        # Parameters - only declare if first one doesn't exist
        if not self.has_parameter("max_range"):
            self.declare_parameter("max_range", 12.0)  # max lidar range
            self.declare_parameter("scan_topic", "/scan_fast")
            self.declare_parameter("auto_localize_timeout", 30.0)  # seconds
            self.declare_parameter("confidence_threshold", 0.95)  # evidence share for 'localized'
            self.declare_parameter("auto_localize", True)  # enable auto-localize on startup
            self.declare_parameter("auto_recover", True)  # relocalize when AMCL loses the robot

        self.max_range = self.get_parameter("max_range").get_parameter_value().double_value
        scan_topic = self.get_parameter("scan_topic").value
        self.auto_timeout = self.get_parameter("auto_localize_timeout").value
        self.confidence_threshold = self.get_parameter("confidence_threshold").get_parameter_value().double_value
        auto_localize = self.get_parameter("auto_localize").value
        self._auto_recover = self.get_parameter("auto_recover").get_parameter_value().bool_value

        # Reset map state
        self.map_received = False
        self.grid = None

        # Latest scan storage
        self.latest_scan = None
        self._scans.clear()
        self._amcl = None
        self._verdict = ""
        self._watch_doubted = False

        # Auto-localize state
        self._auto_done = not auto_localize  # Skip if disabled

        # Subscribers
        scan_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.scan_sub = self.create_subscription(LaserScan, scan_topic, self._scan_cb, scan_qos)

        map_qos = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL
        )
        self.map_sub = self.create_subscription(OccupancyGrid, "/map", self._map_cb, map_qos)
        if self._auto_recover:
            self.amcl_sub = self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose", self._amcl_cb, 1)
            self._odom.clear()
            self.odom_sub = self.create_subscription(Odometry, "/odom", self._odom_cb, 10)

        # Publishers
        # Latched publishers - messages persist for late subscribers (AMCL, the memory recorder)
        # This solves the race condition where grid_localizer publishes before AMCL starts
        latched_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            depth=1,
        )
        self.pose_pub = self.create_lifecycle_publisher(PoseWithCovarianceStamped, "/initialpose", latched_qos)
        self.status_pub = self.create_lifecycle_publisher(String, "/localization/status", latched_qos)

        # Service (manual trigger)
        self.srv = self.create_service(Trigger, "localize", self._localize_cb)
        self.hand_placed_srv = self.create_service(Trigger, "localization/hand_placed", self._hand_placed_cb)

        # AMCL's /set_initial_pose service: the latched /initialpose topic is
        # NOT enough — AMCL subscribes VOLATILE, so a pose published before its
        # subscription activates is lost forever. The service call is retried
        # until AMCL is up.
        self._amcl_seed_client = self.create_client(SetInitialPose, "/set_initial_pose")
        self._seed_retry_timer = None
        self._pending_seed = None

        # Store auto_localize setting for use in on_activate
        self._auto_localize_enabled = auto_localize

        self.get_logger().info("Grid localizer configured. Waiting for map...")

        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Inactive → Active: Enable lifecycle publishers and start auto-localize timer."""
        self.get_logger().info("Grid localizer activated.")

        # Mark node as active
        self._is_active = True

        # Start map check timer if map not received yet
        if not self.map_received and self._map_check_timer is None:
            self._map_check_timer = self.create_timer(1.0, self._check_map_publisher)

        # Start auto-localize timer now that publishers are active
        if self._auto_localize_enabled and self._auto_timer is None:
            self._auto_timer = self.create_timer(0.5, self._auto_localize_tick)
            self.get_logger().info(
                f"Auto-localize enabled: {self.auto_timeout}s timeout, confidence threshold {self.confidence_threshold}"
            )

        if self._auto_recover:
            self._settle()
            if self._watch_timer is None:
                self._watch_timer = self.create_timer(WATCH_PERIOD_S, self._watch_tick)
            else:
                self._watch_timer.reset()
            self._stall_strikes = 0

        # This call automatically activates lifecycle publishers (pose_pub and status_pub)
        return super().on_activate(state)

    def on_deactivate(self, state: State) -> TransitionCallbackReturn:
        """Active → Inactive: Disable lifecycle publishers and stop auto-localize timer."""
        # Mark node as inactive
        self._is_active = False

        # Cancel timers first
        if self._auto_timer:
            self._auto_timer.cancel()
        if self._map_check_timer:
            self._map_check_timer.cancel()
        if self._watch_timer:
            self._watch_timer.cancel()
        self._drop_stall_fix()
        # Drop any pending AMCL seed: across a deactivate/reactivate (e.g. a
        # map switch) a surviving retry would deliver the PREVIOUS map's pose.
        if self._seed_retry_timer:
            self.destroy_timer(self._seed_retry_timer)
            self._seed_retry_timer = None
        self._pending_seed = None

        self.get_logger().info("Grid localizer deactivated.")
        # This call automatically deactivates lifecycle publishers (pose_pub and status_pub)
        return super().on_deactivate(state)

    def _cleanup_resources(self):
        """Helper method to clean up all node resources."""
        self.get_logger().info("Attempting to clean up Grid Localizer")
        # Destroy timers
        if self._auto_timer:
            self.destroy_timer(self._auto_timer)
            self._auto_timer = None
        if self._map_check_timer:
            self.destroy_timer(self._map_check_timer)
            self._map_check_timer = None
        if self._seed_retry_timer:
            self.destroy_timer(self._seed_retry_timer)
            self._seed_retry_timer = None
        if self._watch_timer:
            self.destroy_timer(self._watch_timer)
            self._watch_timer = None
        self._drop_stall_fix()
        # Clear the pending seed too: an in-flight /set_initial_pose failing
        # after cleanup would otherwise see it as current and re-arm a retry
        # timer on a cleaned-up node.
        self._pending_seed = None

        # Destroy service
        if self.srv:
            self.destroy_service(self.srv)
            self.srv = None
        if self.hand_placed_srv:
            self.destroy_service(self.hand_placed_srv)
            self.hand_placed_srv = None

        # Destroy publishers
        if self.pose_pub:
            self.destroy_publisher(self.pose_pub)
            self.pose_pub = None
        if self.status_pub:
            self.destroy_publisher(self.status_pub)
            self.status_pub = None

        # Destroy subscriptions
        if self.scan_sub:
            self.destroy_subscription(self.scan_sub)
            self.scan_sub = None
        if self.map_sub:
            self.destroy_subscription(self.map_sub)
            self.map_sub = None
        if self.amcl_sub:
            self.destroy_subscription(self.amcl_sub)
            self.amcl_sub = None
        if self.odom_sub:
            self.destroy_subscription(self.odom_sub)
            self.odom_sub = None
        self.grid = None

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Inactive → Unconfigured: Destroy all resources."""
        self._cleanup_resources()
        self.get_logger().info("Grid localizer cleaned up.")
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """Any State → Finalized: Destroy all resources (can be called from any state)."""
        self._cleanup_resources()
        self.get_logger().info("Grid localizer shut down.")
        return TransitionCallbackReturn.SUCCESS

    def _check_map_publisher(self):
        """Periodically check if /map topic has an active publisher."""
        if self.map_received:
            # Already got the map, stop checking
            self._map_check_timer.cancel()
            return

        # Check how many publishers are on /map
        pub_count = self.count_publishers("/map")
        if pub_count == 0:
            self.get_logger().warn(
                "Waiting for /map publisher - map_server may not be activated yet", throttle_duration_sec=5.0
            )
        else:
            # Publisher exists but no data - map_server is likely in configured (not active) state
            self.get_logger().warn(
                f"/map has {pub_count} publisher(s) but no data received. "
                f'map_server may be stuck in "configured" state (not "active"). '
                f"Check: ros2 lifecycle get /map_server",
                throttle_duration_sec=5.0,
            )

    def _map_cb(self, msg: OccupancyGrid):
        """Handle incoming map updates."""
        if self.map_received:
            self.get_logger().info(
                f"Received map update: {msg.info.width}x{msg.info.height}, res={msg.info.resolution}"
            )
            # Reset auto-localization state for new map
            self._auto_done = False
            # Re-enable auto-localize timer (restart the cancelled timer)
            if self._auto_localize_enabled and self._auto_timer:
                self._auto_timer.reset()
                self.get_logger().info("Re-enabled auto-localize timer for new map")
        else:
            self.get_logger().info(f"Received map: {msg.info.width}x{msg.info.height}, res={msg.info.resolution}")

        self._publish_status("processing_map")
        self.grid = Grid.from_occupancy_grid(msg)
        self._settle()

        # Record time map was received to allow for a startup delay
        self.map_received_time = self.get_clock().now()
        self.map_received = True

    def _scan_cb(self, msg: LaserScan):
        """Store latest scan."""
        self.latest_scan = msg
        self._scans.append(msg)
        self._scan_received_at = time.monotonic()
        if self._auto_recover and self._is_active:
            self._check_stall()

    def _auto_localize_tick(self):
        """Auto-localize on startup.

        Runs the full search in one blocking call for maximum speed.
        The search runs on the CPU and takes about half a second on the Jetson.
        Retries on insufficient scan data, but gives up on other errors.
        """
        if self._auto_done:
            return

        # Need scan data and map before we start
        if not self.map_received:
            return

        if self.latest_scan is None:
            self.get_logger().info("Waiting for scan data...", throttle_duration_sec=2.0)
            return

        self.get_logger().info("Starting auto-localization search...")
        start_time = self.get_clock().now()

        try:
            estimate = self._find_pose(self.latest_scan)
            elapsed = (self.get_clock().now() - start_time).nanoseconds / 1e9

            # Success - stop retrying
            self._auto_done = True
            if self._auto_timer:
                self._auto_timer.cancel()

            pose = estimate.pose
            self._publish_pose(pose.x, pose.y, pose.theta, self.latest_scan.header.stamp)
            self._warn_if_at_map_edge(pose.x, pose.y)

            if estimate.confident(self.confidence_threshold):
                self._publish_status("localized")
                self.get_logger().info(
                    f"Auto-localized with high confidence at {_describe(estimate)} in {elapsed:.2f}s"
                )
            else:
                self._publish_status("localized_low_confidence")
                self.get_logger().warn(
                    f"Auto-localized with LOW confidence at {_describe(estimate)} "
                    f"(threshold: {self.confidence_threshold:.0%}) in {elapsed:.2f}s "
                    f"— another place on the map fits the scan nearly as well, or the map may not match the "
                    f"robot's surroundings; please check the robot's position on the map, or remap it"
                )
        except ValueError as e:
            # Insufficient scan data - retry on next tick
            self.get_logger().warn(f"Scan insufficient, will retry: {e}", throttle_duration_sec=2.0)
        except Exception as e:
            # Other errors - give up
            self._auto_done = True
            if self._auto_timer:
                self._auto_timer.cancel()
            self._publish_status("error")
            self.get_logger().error(f"Auto-localization failed: {e}")

    def _publish_status(self, status: str):
        """Publish status for app to consume."""
        if self.status_pub is None or not self.status_pub.is_activated:
            self.get_logger().warn(f"Status publisher is not active. Cannot publish status: {status}")
            return

        msg = String()
        msg.data = status
        self.status_pub.publish(msg)
        self.get_logger().info(f"Published status: {status}")
        if status in ("localized", "localized_low_confidence"):
            self._verdict = status
            self._watch_doubted = False

    EDGE_MARGIN_M = 0.30  # closer to the border than this and the costmap can't see ahead

    def _warn_if_at_map_edge(self, x: float, y: float) -> None:
        """Log one clear hint when the localized pose hugs the map border."""
        grid = self.grid
        if grid is None:
            return
        h, w = grid.shape
        dist = min(
            x - grid.origin_x,
            grid.origin_x + w * grid.resolution - x,
            y - grid.origin_y,
            grid.origin_y + h * grid.resolution - y,
        )
        if dist < self.EDGE_MARGIN_M:
            self.get_logger().warn(
                f"Localized {max(dist, 0.0):.2f} m from the map border — please make sure the map is "
                "correct and the robot is not right against a wall or outside the mapped area "
                "(the costmap cannot see past the map edge, so navigation is unreliable there)"
            )

    def _publish_pose(self, x: float, y: float, theta: float, stamp: Time | None = None):
        """Publish pose to /initialpose (latched for AMCL). Stamped with the scan it was matched from,
        AMCL adds the odometry since; stamped now, it takes the pose as current (a stalled robot's
        odometry since the scan is phantom)."""
        if self.pose_pub is None or not self.pose_pub.is_activated:
            self.get_logger().warn("Pose publisher is not active. Cannot publish pose.")
            return

        msg = PoseWithCovarianceStamped()
        msg.header.stamp = self.get_clock().now().to_msg() if stamp is None else stamp
        msg.header.frame_id = "map"
        msg.pose.pose.position.x = x
        msg.pose.pose.position.y = y
        msg.pose.pose.orientation.z = np.sin(theta / 2)
        msg.pose.pose.orientation.w = np.cos(theta / 2)
        msg.pose.covariance[0] = 0.1
        msg.pose.covariance[7] = 0.1
        msg.pose.covariance[35] = 0.05
        self.pose_pub.publish(msg)
        self.get_logger().info(f"Published initial pose: ({x:.2f}, {y:.2f})")
        self._settle()
        self._seed_amcl(msg)

    def _seed_amcl(self, pose_msg: PoseWithCovarianceStamped, retries_left: int = 15):
        """Deliver the pose to AMCL via its /set_initial_pose service.

        Retries every 2 s while AMCL isn't up yet AND when a call fails
        (boot/mode-switch races, transient service errors); a newer pose
        always supersedes a pending retry, and a stale in-flight call's
        completion never touches the newer pose's pending state.
        """
        self._pending_seed = (pose_msg, retries_left)
        if self._seed_retry_timer is not None:
            self.destroy_timer(self._seed_retry_timer)
            self._seed_retry_timer = None

        if retries_left <= 0:
            self.get_logger().warning("Giving up on AMCL /set_initial_pose; seed delivered by latched topic only")
            self._pending_seed = None
            return

        if not self._amcl_seed_client.service_is_ready():
            self._seed_retry_timer = self.create_timer(2.0, self._retry_seed_amcl)
            return

        request = SetInitialPose.Request()
        request.pose = pose_msg
        future = self._amcl_seed_client.call_async(request)

        def _on_done(completed):
            # A newer pose may have been queued while this call was in flight;
            # its retry state is not ours to clear or reschedule.
            superseded = self._pending_seed is None or self._pending_seed[0] is not pose_msg
            if not superseded and self._seed_retry_timer is not None:
                # Disarm the in-flight watchdog: the call did complete.
                self.destroy_timer(self._seed_retry_timer)
                self._seed_retry_timer = None
            try:
                completed.result()
                self.get_logger().info("Seeded AMCL via /set_initial_pose")
                if not superseded:
                    self._pending_seed = None
            except Exception as e:
                self.get_logger().warning(f"AMCL /set_initial_pose call failed: {e}")
                if not superseded and self._seed_retry_timer is None:
                    self._seed_retry_timer = self.create_timer(2.0, self._retry_seed_amcl)

        def _watchdog():
            # call_async futures never time out on their own, and AMCL dying
            # after accepting the request would otherwise strand the seed with
            # no retry (service_is_ready() was true when we sent it).
            self.get_logger().warning("AMCL /set_initial_pose reply overdue; retrying")
            self._amcl_seed_client.remove_pending_request(future)
            self._retry_seed_amcl()

        # Arm the watchdog before the done-callback so an immediately-completed
        # future disarms it rather than racing it.
        self._seed_retry_timer = self.create_timer(5.0, _watchdog)
        future.add_done_callback(_on_done)

    def _retry_seed_amcl(self):
        if self._seed_retry_timer is not None:
            self.destroy_timer(self._seed_retry_timer)
            self._seed_retry_timer = None
        if self._pending_seed is None:
            return
        pose_msg, retries_left = self._pending_seed
        self._seed_amcl(pose_msg, retries_left - 1)

    def _localize_cb(self, request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        """Service callback to trigger localization."""
        # Check if node is active
        if not self._is_active:
            response.success = False
            response.message = "Node not active"
            return response

        if not self.map_received:
            response.success = False
            response.message = "No map received yet"
            return response

        if self.latest_scan is None:
            response.success = False
            response.message = "No scan received yet"
            return response

        try:
            estimate = self._find_pose(self.latest_scan)
            pose = estimate.pose

            self._publish_pose(pose.x, pose.y, pose.theta, self.latest_scan.header.stamp)
            self._warn_if_at_map_edge(pose.x, pose.y)

            confident = estimate.confident(self.confidence_threshold)
            self._publish_status("localized" if confident else "localized_low_confidence")
            response.success = True
            confidence = "Localized" if confident else LOW_CONFIDENCE
            response.message = f"{confidence} at {_describe(estimate)}"
            self.get_logger().info(response.message)

        except Exception as e:
            response.success = False
            response.message = str(e)
            self.get_logger().error(f"Localization failed: {e}")

        return response

    def _hand_placed_cb(self, request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        """A person placed the robot on the map from the app, vouching for the pose: record that
        as the latched verdict, so a late subscriber replays it instead of an earlier doubt."""
        response.success = self._is_active
        if not self._is_active:
            response.message = "Node not active"
            return response
        self._publish_status("localized")
        self._settle()
        response.message = "Hand placement recorded"
        return response

    def _amcl_cb(self, msg: PoseWithCovarianceStamped) -> None:
        pose = msg.pose.pose
        theta = 2.0 * math.atan2(pose.orientation.z, pose.orientation.w)
        self._amcl = (_stamp(msg), Pose2D(pose.position.x, pose.position.y, theta))

    def _settle(self) -> None:
        """A new seed or map: forget AMCL's earlier estimate, give the new one time to take, and drop a
        pending stall correction, which belongs to the pose and map before it."""
        self._amcl = None
        self._lost_strikes = 0
        self._watch_quiet_until = time.monotonic() + SETTLE_S
        self._drop_stall_fix()

    def _watch_tick(self) -> None:
        """Count AMCL as lost when even the best pose near its own leaves the scan unexplained.
        Not judged on unexplored floor (the planner crosses it): the surroundings there are not on the map."""
        if self.grid is None or time.monotonic() < self._watch_quiet_until:
            return
        paired = self._scan_for_amcl_pose()
        if paired is None:
            return
        msg, believed = paired
        if not self.grid.explored(believed.x, believed.y):
            self._lost_strikes = 0
            return
        scan = Scan.from_laser_scan(msg, self.max_range)
        if len(scan) < MIN_SCAN_POINTS:
            return
        here = refine(self.grid, scan, believed)
        if here.fit >= MIN_FIT:
            self._lost_strikes = 0
            if self._watch_doubted and time.monotonic() >= self._reaffirm_after:
                self._reaffirm(here)
            return
        self._lost_strikes += 1
        if self._lost_strikes >= LOST_STRIKES:
            self._recover(here)

    def _reaffirm(self, here: Pose2D) -> None:
        """A doubted pose fits the scan again: restore 'localized' only when the map-wide search
        confidently agrees it is the one place that does, since a wrong place can fit locally too."""
        scan = self.latest_scan
        if scan is None:
            return
        try:
            estimate = self._find_pose(scan)
        except (ValueError, RuntimeError):
            estimate = None
        if estimate is not None and estimate.confident(self.confidence_threshold) and _same_place(estimate.pose, here):
            self._publish_status("localized")
            return
        self._reaffirm_after = time.monotonic() + RETRY_S

    def _scan_for_amcl_pose(self) -> tuple[LaserScan, Pose2D] | None:
        """The scan AMCL's latest estimate came from. AMCL re-estimates on any odometry change, so an
        estimate older than every buffered scan means the robot has not moved: the newest scan stands in."""
        if self._amcl is None or not self._scans:
            return None
        stamp, believed = self._amcl
        if stamp < _stamp(self._scans[0]):
            return self._scans[-1], believed
        match = min(self._scans, key=lambda scan: abs(_stamp(scan) - stamp))
        return (match, believed) if abs(_stamp(match) - stamp) < SAME_SCAN_S else None

    def _recover(self, here: Pose2D) -> None:
        """Move AMCL only to a confident match that explains clearly more of the scan than where it is:
        a crowd around the robot or moved furniture lowers every pose's fit alike."""
        self._lost_strikes = 0
        self._watch_quiet_until = time.monotonic() + RETRY_S
        lost_at = f"{describe_pose(here.x, here.y, here.theta)}, lidar fit {here.fit:.0%}"
        scan = self.latest_scan
        if scan is None:
            return
        try:
            estimate = self._find_pose(scan)
        except (ValueError, RuntimeError) as e:
            self.get_logger().warn(f"AMCL looks lost at {lost_at}, and the map-wide search failed: {e}")
            self._doubt()
            return
        if not estimate.confident(self.confidence_threshold) or estimate.pose.fit < here.fit + RECOVERY_MARGIN:
            self.get_logger().warn(
                f"AMCL's pose {lost_at} explains little of the scan, but no place on the map clearly does "
                f"better (best: {_describe(estimate)}); keeping it: the map may be incomplete here or the "
                f"surroundings changed. Next check in {RETRY_S:.0f} s"
            )
            self._doubt()
            return
        self.get_logger().warn(f"AMCL lost the robot at {lost_at}; relocalized at {_describe(estimate)}")
        pose = estimate.pose
        self._publish_pose(pose.x, pose.y, pose.theta, scan.header.stamp)
        self._publish_status("localized")

    def _doubt(self) -> None:
        """AMCL's pose is judged wrong but nothing better was found: withdraw a 'localized' verdict, so
        the memory recorder stops saving views under it, until the pose fits again or someone relocalizes."""
        if self._verdict != "localized":
            return
        self._publish_status("localized_low_confidence")
        self._watch_doubted = True

    def _odom_cb(self, msg: Odometry) -> None:
        pose = msg.pose.pose
        theta = 2.0 * math.atan2(pose.orientation.z, pose.orientation.w)
        self._odom.append((_stamp(msg), Pose2D(pose.position.x, pose.position.y, theta)))
        self._odom_received_at = time.monotonic()

    def _odom_at(self, stamp: float) -> Pose2D | None:
        if not self._odom:
            return None
        at, pose = min(self._odom, key=lambda sample: abs(sample[0] - stamp))
        return pose if abs(at - stamp) <= ODOM_MATCH_S else None

    def _check_stall(self) -> None:
        """Count a stall when the wheels claim motion that the lidar shows did not happen."""
        if time.monotonic() < self._stall_quiet_until:
            return
        claim = self._wheel_claim()
        if claim is None:
            self._stall_strikes = 0
            return
        before, after, claimed = claim
        earlier, now = Scan.from_laser_scan(before, STALL_RANGE_M), Scan.from_laser_scan(after, STALL_RANGE_M)
        if min(len(earlier), len(now)) < MIN_SCAN_POINTS:
            self._stall_strikes = 0
            return
        field = Grid.from_scan(earlier, STALL_GRID_M)
        still = evaluate(field, now, 0.0, 0.0, 0.0, STALL_INLIER_M)
        moved = evaluate(field, now, claimed.x, claimed.y, claimed.theta, STALL_INLIER_M)
        if still.fit < MIN_FIT or still.fit < moved.fit + STALL_MARGIN:
            self._stall_strikes = 0
            return
        self._stall_strikes += 1
        if self._stall_strikes >= STALL_STRIKES:
            self._on_stall(claimed, still.fit, moved.fit, _stamp(after) - _stamp(before))

    def _wheel_claim(self) -> tuple[LaserScan, LaserScan, Pose2D] | None:
        """The newest scan, the latest earlier one the wheels claim to have clearly moved away from,
        and that claimed motion in the earlier scan's frame; None when the wheels claim no such move."""
        if len(self._scans) < 2 or not self._odom or self._odom[0][1] == self._odom[-1][1]:
            return None  # parked wheels report the same pose for the whole buffer
        after = self._scans[-1]
        end = self._odom_at(_stamp(after))
        if end is None:
            return None
        for before in reversed(list(self._scans)[:-1]):
            if _stamp(after) - _stamp(before) > STALL_LOOKBACK_S:
                return None
            start = self._odom_at(_stamp(before))
            if start is None:
                continue
            claimed = end.relative_to(start)
            if math.hypot(claimed.x, claimed.y) >= STALL_MIN_MOVE_M or abs(claimed.theta) >= STALL_MIN_TURN:
                return before, after, claimed
        return None

    def _on_stall(self, claimed: Pose2D, still_fit: float, moved_fit: float, window: float) -> None:
        self._stall_strikes = 0
        self._stall_quiet_until = time.monotonic() + STALL_QUIET_S
        self.get_logger().error(
            f"Stalled: the wheels claim {math.hypot(claimed.x, claimed.y):.2f} m and "
            f"{math.degrees(abs(claimed.theta)):.0f}° in {window:.1f} s, but the lidar shows the robot standing "
            f"still ({still_fit:.0%} of the scan unchanged, {moved_fit:.0%} where the wheels claim); "
            "cancelling navigation and relocalizing"
        )
        if self._cancel_nav_client.service_is_ready():
            self._cancel_nav_client.call_async(Trigger.Request()).add_done_callback(self._on_nav_cancel_reply)
        else:
            self.get_logger().warn("/nav/cancel_navigation is unavailable; correcting AMCL once the wheels stop")
        if self._stall_fix_timer is None:
            self._stall_fix_deadline = time.monotonic() + STALL_FIX_TIMEOUT_S
            self._stall_fix_timer = self.create_timer(STALL_SETTLE_S, self._relocalize_after_stall)

    def _on_nav_cancel_reply(self, future: Future) -> None:
        try:
            reply = future.result()
        except Exception as e:  # noqa: BLE001 — the reply is informational; the wheels decide when to correct
            self.get_logger().warn(f"/nav/cancel_navigation failed: {e}")
            return
        if reply is not None and not reply.success:
            self.get_logger().warn(f"/nav/cancel_navigation refused: {reply.message}")

    def _drop_stall_fix(self) -> None:
        if self._stall_fix_timer is not None:
            self.destroy_timer(self._stall_fix_timer)
            self._stall_fix_timer = None

    def _wheels_turning(self, window_s: float) -> bool:
        """Whether wheel odometry moved within the last window_s; True too while odometry cannot tell,
        because it stopped arriving or the buffer is younger than the window."""
        if not self._odom or time.monotonic() - self._odom_received_at > window_s:
            return True
        latest_at, latest = self._odom[-1]
        for at, pose in reversed(self._odom):
            if latest_at - at > window_s:
                return False
            if pose.distance(latest) > 0.005 or pose.heading_gap(latest) > 0.005:
                return True
        return True

    def _relocalize_after_stall(self) -> None:
        """The spinning wheels dragged AMCL while the robot stood still: once they stop (a cancelled goal
        keeps driving them until it ends), put AMCL back where the scan says."""
        if self._wheels_turning(STALL_SETTLE_S):
            if time.monotonic() < self._stall_fix_deadline:
                return
            self._drop_stall_fix()
            self.get_logger().warn(
                f"Wheel odometry did not show the base still within {STALL_FIX_TIMEOUT_S:.0f} s of the stall "
                "(navigation did not stop, or odometry stopped arriving); leaving AMCL to the next stall check"
            )
            return
        self._drop_stall_fix()
        paired = self._scan_for_amcl_pose()
        if self.grid is None or paired is None:
            return
        msg, believed = paired
        scan = Scan.from_laser_scan(msg, self.max_range)
        if len(scan) < MIN_SCAN_POINTS:
            return
        here = refine(
            self.grid, scan, believed, span_m=STALL_SEARCH_M, step_m=0.05, span_deg=STALL_SEARCH_DEG, step_deg=2.5
        )
        if here.fit < MIN_FIT:
            self._recover(here)
            return
        if here.distance(believed) <= STALL_DRIFT_M and here.heading_gap(believed) <= STALL_DRIFT_TURN:
            return
        self.get_logger().warn(
            f"After the stall AMCL was at ({believed.x:.2f}, {believed.y:.2f}); the scan puts the robot at "
            f"{describe_pose(here.x, here.y, here.theta)}, lidar fit {here.fit:.0%}"
        )
        self._publish_pose(here.x, here.y, here.theta)

    def _find_pose(self, msg: LaserScan) -> Estimate:
        """The best pose for this scan over the whole map, and how sure it is."""
        if self.grid is None:
            raise RuntimeError("No map received yet")
        age = time.monotonic() - self._scan_received_at
        if age > MAX_SCAN_AGE_S:
            raise ValueError(f"The latest scan is {age:.1f} s old; is the lidar running?")
        scan = Scan.from_laser_scan(msg, self.max_range)
        if len(scan) < MIN_SCAN_POINTS:
            raise ValueError("Not enough valid scan points")
        estimate = locate(self.grid, scan)
        if estimate is None:
            raise RuntimeError("No pose on the map fits the scan")
        return estimate


def _stamp(msg: LaserScan | PoseWithCovarianceStamped | Odometry) -> float:
    return msg.header.stamp.sec + msg.header.stamp.nanosec * 1e-9


def _same_place(a: Pose2D, b: Pose2D) -> bool:
    return a.distance(b) < MERGE_M and a.heading_gap(b) < math.radians(MERGE_DEG)


def _describe(estimate: Estimate) -> str:
    pose = estimate.pose
    return (
        f"{describe_pose(pose.x, pose.y, pose.theta)}, {estimate.share:.0%} of the evidence, lidar fit {pose.fit:.0%}"
    )


def main(args=None):
    rclpy.init(args=args)
    lc_node = GridLocalizer("grid_localizer")
    try:
        rclpy.spin(lc_node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        rclpy.shutdown()


if __name__ == "__main__":
    main()
