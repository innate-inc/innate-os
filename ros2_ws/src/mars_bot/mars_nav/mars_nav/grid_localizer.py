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
- Runs as a lifecycle node for proper initialization coordination

On startup, automatically localizes once a map and a scan are in.
Publishes status to /localization/status: 'localized' when the best pose holds at
least `confidence_threshold` of the evidence (other places that fit the scan nearly
as well take the rest), else 'localized_low_confidence'.
Service remains available for manual triggers after auto-localize completes.
"""

import time

import numpy as np
import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav2_msgs.srv import SetInitialPose
from nav_msgs.msg import OccupancyGrid
from rclpy.executors import ExternalShutdownException
from rclpy.lifecycle import Node, Publisher, State, TransitionCallbackReturn
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import LaserScan
from std_msgs.msg import String
from std_srvs.srv import Trigger

from mars_nav.scan_match import Estimate, Grid, Scan, locate

MAX_SCAN_AGE_S = 1.0  # /scan_fast runs at ~10 Hz: anything older means the lidar stopped
LOW_CONFIDENCE = "Localized with LOW confidence"  # /localize reply prefix the relocalize skill keys on


class GridLocalizer(Node):
    """Global lidar localization lifecycle node."""

    # Subscriptions and publishers (created in on_configure)
    scan_sub: rclpy.subscription.Subscription | None = None
    map_sub: rclpy.subscription.Subscription | None = None
    pose_pub: Publisher | None = None
    status_pub: Publisher | None = None
    srv = None
    _auto_timer = None
    _map_check_timer = None

    # Map state
    map_received: bool = False
    grid: Grid | None = None
    map_received_time = None

    # Scan storage
    latest_scan = None
    _scan_received_at = 0.0

    # Auto-localize state
    _auto_done: bool = False
    _auto_localize_enabled: bool = False

    # Node state tracking
    _is_active: bool = False

    # Parameters (declared in on_configure)
    max_range: float
    auto_timeout = None
    confidence_threshold: float

    def __init__(self, node_name="grid_localizer", **kwargs):
        super().__init__(node_name, **kwargs)

    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Unconfigured → Inactive: Declare parameters and create resources."""
        # Parameters - only declare if first one doesn't exist
        if not self.has_parameter("max_range"):
            self.declare_parameter("max_range", 12.0)  # max lidar range
            self.declare_parameter("scan_topic", "/scan_fast")
            self.declare_parameter("auto_localize_timeout", 30.0)  # seconds
            self.declare_parameter("confidence_threshold", 0.95)  # evidence share for 'localized'
            self.declare_parameter("auto_localize", True)  # enable auto-localize on startup

        self.max_range = self.get_parameter("max_range").get_parameter_value().double_value
        scan_topic = self.get_parameter("scan_topic").value
        self.auto_timeout = self.get_parameter("auto_localize_timeout").value
        self.confidence_threshold = self.get_parameter("confidence_threshold").get_parameter_value().double_value
        auto_localize = self.get_parameter("auto_localize").value

        # Reset map state
        self.map_received = False
        self.grid = None

        # Latest scan storage
        self.latest_scan = None

        # Auto-localize state
        self._auto_done = not auto_localize  # Skip if disabled

        # Subscribers
        scan_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.scan_sub = self.create_subscription(LaserScan, scan_topic, self._scan_cb, scan_qos)

        map_qos = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.TRANSIENT_LOCAL
        )
        self.map_sub = self.create_subscription(OccupancyGrid, "/map", self._map_cb, map_qos)

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
        # Clear the pending seed too: an in-flight /set_initial_pose failing
        # after cleanup would otherwise see it as current and re-arm a retry
        # timer on a cleaned-up node.
        self._pending_seed = None

        # Destroy service
        if self.srv:
            self.destroy_service(self.srv)
            self.srv = None

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

        # Record time map was received to allow for a startup delay
        self.map_received_time = self.get_clock().now()
        self.map_received = True

    def _scan_cb(self, msg: LaserScan):
        """Store latest scan."""
        self.latest_scan = msg
        self._scan_received_at = time.monotonic()

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
            self._publish_pose(pose.x, pose.y, pose.theta)
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

    def _publish_pose(self, x: float, y: float, theta: float):
        """Publish pose to /initialpose (latched for AMCL)."""
        if self.pose_pub is None or not self.pose_pub.is_activated:
            self.get_logger().warn("Pose publisher is not active. Cannot publish pose.")
            return

        msg = PoseWithCovarianceStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
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

    def _localize_cb(self, request, response):
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

            self._publish_pose(pose.x, pose.y, pose.theta)
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

    def _find_pose(self, msg: LaserScan) -> Estimate:
        """The best pose for this scan over the whole map, and how sure it is."""
        if self.grid is None:
            raise RuntimeError("No map received yet")
        age = time.monotonic() - self._scan_received_at
        if age > MAX_SCAN_AGE_S:
            raise ValueError(f"The latest scan is {age:.1f} s old; is the lidar running?")
        scan = Scan.from_laser_scan(msg, self.max_range)
        if len(scan) < 10:
            raise ValueError("Not enough valid scan points")
        estimate = locate(self.grid, scan)
        if estimate is None:
            raise RuntimeError("No pose on the map fits the scan")
        return estimate


def _describe(estimate: Estimate) -> str:
    pose = estimate.pose
    return (
        f"({pose.x:.2f}, {pose.y:.2f}, {np.degrees(pose.theta):.1f}°), "
        f"{estimate.share:.0%} of the evidence, lidar fit {pose.fit:.0%}"
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
