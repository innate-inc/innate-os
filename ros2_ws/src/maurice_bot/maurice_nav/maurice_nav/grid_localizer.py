#!/usr/bin/env python3
"""
GPU-accelerated grid localization for initial pose estimation.
Uses CuPy for parallel ray-casting to find robot pose in occupancy grid map.

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
- Subscribes to /map and /scan
- Generates candidate poses on a grid (configurable spacing and angles)
- Scores each pose by matching laser scan endpoints to map obstacles
- Publishes best pose to /initialpose with transient_local QoS (latched)
- Runs as a lifecycle node for proper initialization coordination

On startup, automatically tries to localize for up to `auto_localize_timeout` seconds.
Publishes status to /localization/status ('localized' or 'timeout').
Service remains available for manual triggers after auto-localize completes.
"""

from typing import Optional

import numpy as np
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.executors import SingleThreadedExecutor
from rclpy.lifecycle import Node
from rclpy.lifecycle import Publisher
from rclpy.lifecycle import State
from rclpy.lifecycle import TransitionCallbackReturn
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from sensor_msgs.msg import LaserScan
from nav_msgs.msg import OccupancyGrid
from geometry_msgs.msg import PoseWithCovarianceStamped
from std_srvs.srv import Trigger
from std_msgs.msg import String

import cupy as cp


class GridLocalizer(Node):
    """GPU-accelerated grid localization lifecycle node."""
    
    # Subscriptions and publishers (created in on_configure)
    scan_sub: Optional[rclpy.subscription.Subscription] = None
    map_sub: Optional[rclpy.subscription.Subscription] = None
    pose_pub: Optional[Publisher] = None
    status_pub: Optional[Publisher] = None
    srv = None
    _auto_timer = None
    _map_check_timer = None
    
    # Map state
    map_received: bool = False
    map_free = None
    map_free_gpu = None
    resolution = None
    origin = None
    map_h = None
    map_w = None
    free_pixels = None
    map_received_time = None
    
    # Scan storage
    latest_scan = None
    
    # Auto-localize state
    _auto_done: bool = False
    _auto_localize_enabled: bool = False
    
    # Node state tracking
    _is_active: bool = False
    
    # Parameters (declared in on_configure)
    sample_dist = None
    angle_samples = None
    max_range = None
    batch_size = None
    auto_timeout = None
    score_threshold = None
    
    def __init__(self, node_name='grid_localizer', **kwargs):
        super().__init__(node_name, **kwargs)
    def on_configure(self, state: State) -> TransitionCallbackReturn:
        """Unconfigured → Inactive: Declare parameters and create resources."""
        # Parameters - only declare if first one doesn't exist
        if not self.has_parameter('sample_distance'):
            self.declare_parameter('sample_distance', 0.15)  # meters between samples
            self.declare_parameter('angle_samples', 36)  # angles to try (360/36 = 10° increments)
            self.declare_parameter('batch_size', 4000)  # poses per GPU batch (reduced for memory)
            self.declare_parameter('max_range', 12.0)  # max lidar range
            self.declare_parameter('scan_topic', '/scan_fast')
            self.declare_parameter('auto_localize_timeout', 30.0)  # seconds
            self.declare_parameter('max_score_threshold', 0.3)  # lower = stricter match
            self.declare_parameter('auto_localize', True)  # enable auto-localize on startup
        
        self.sample_dist = self.get_parameter('sample_distance').value
        self.angle_samples = self.get_parameter('angle_samples').value
        self.max_range = self.get_parameter('max_range').value
        self.batch_size = self.get_parameter('batch_size').value
        scan_topic = self.get_parameter('scan_topic').value
        self.auto_timeout = self.get_parameter('auto_localize_timeout').value
        self.score_threshold = self.get_parameter('max_score_threshold').value
        auto_localize = self.get_parameter('auto_localize').value
        
        # Reset map state
        self.map_received = False
        self.map_free = None
        self.map_free_gpu = None
        self.resolution = None
        self.origin = None
        
        # Latest scan storage
        self.latest_scan = None
        
        # Auto-localize state
        self._auto_done = not auto_localize  # Skip if disabled
        
        # Subscribers
        scan_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.scan_sub = self.create_subscription(LaserScan, scan_topic, self._scan_cb, scan_qos)
        
        map_qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL
        )
        self.map_sub = self.create_subscription(OccupancyGrid, '/map', self._map_cb, map_qos)
        
        # Publishers
        # Latched publisher - message persists for late subscribers (AMCL)
        # This solves the race condition where grid_localizer publishes before AMCL starts
        latched_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            depth=1,
        )
        self.pose_pub = self.create_lifecycle_publisher(PoseWithCovarianceStamped, '/initialpose', latched_qos)
        self.status_pub = self.create_lifecycle_publisher(String, '/localization/status', 10)
        
        # Service (manual trigger)
        self.srv = self.create_service(Trigger, 'localize', self._localize_cb)
        
        # Store auto_localize setting for use in on_activate
        self._auto_localize_enabled = auto_localize
        
        self.get_logger().info('Grid localizer configured. Waiting for map...')
        
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state: State) -> TransitionCallbackReturn:
        """Inactive → Active: Enable lifecycle publishers and start auto-localize timer."""
        self.get_logger().info('Grid localizer activated.')
        
        # Mark node as active
        self._is_active = True
        
        # Start map check timer if map not received yet
        if not self.map_received and self._map_check_timer is None:
            self._map_check_timer = self.create_timer(1.0, self._check_map_publisher)
        
        # Start auto-localize timer now that publishers are active
        if self._auto_localize_enabled and self._auto_timer is None:
            self._auto_timer = self.create_timer(0.5, self._auto_localize_tick)
            self.get_logger().info(f'Auto-localize enabled: {self.auto_timeout}s timeout, score threshold {self.score_threshold}')
        
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
        
        self.get_logger().info('Grid localizer deactivated.')
        # This call automatically deactivates lifecycle publishers (pose_pub and status_pub)
        return super().on_deactivate(state)

    def _cleanup_resources(self):
        """Helper method to clean up all node resources."""
        self.get_logger().info('Attempting to clean up Grid Localizer')
        # Destroy timers
        if self._auto_timer:
            self.destroy_timer(self._auto_timer)
            self._auto_timer = None
        if self._map_check_timer:
            self.destroy_timer(self._map_check_timer)
            self._map_check_timer = None
        
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
        
        # Clean up GPU memory
        if self.map_free_gpu is not None:
            del self.map_free_gpu
            self.map_free_gpu = None
            cp.get_default_memory_pool().free_all_blocks()

    def on_cleanup(self, state: State) -> TransitionCallbackReturn:
        """Inactive → Unconfigured: Destroy all resources and free GPU memory."""
        self._cleanup_resources()
        self.get_logger().info('Grid localizer cleaned up.')
        return TransitionCallbackReturn.SUCCESS

    def on_shutdown(self, state: State) -> TransitionCallbackReturn:
        """Any State → Finalized: Destroy all resources (can be called from any state)."""
        self._cleanup_resources()
        self.get_logger().info('Grid localizer shut down.')
        return TransitionCallbackReturn.SUCCESS

    def _check_map_publisher(self):
        """Periodically check if /map topic has an active publisher."""
        if self.map_received:
            # Already got the map, stop checking
            self._map_check_timer.cancel()
            return
        
        # Check how many publishers are on /map
        pub_count = self.count_publishers('/map')
        if pub_count == 0:
            self.get_logger().warn('Waiting for /map publisher - map_server may not be activated yet', throttle_duration_sec=5.0)
        else:
            # Publisher exists but no data - map_server is likely in configured (not active) state
            self.get_logger().warn(
                f'/map has {pub_count} publisher(s) but no data received. '
                f'map_server may be stuck in "configured" state (not "active"). '
                f'Check: ros2 lifecycle get /map_server',
                throttle_duration_sec=5.0
            )

    def _map_cb(self, msg: OccupancyGrid):
        """Handle incoming map updates."""
        # Note: cupy imported at module level
        
        # Check if this is a map update (second map)
        is_map_update = self.map_received
        
        if is_map_update:
            self.get_logger().info(f'Received map update: {msg.info.width}x{msg.info.height}, res={msg.info.resolution}')
            
            # Free old resources before reassigning
            if self.map_free_gpu is not None:
                self.get_logger().info('Freeing old GPU map memory')
                del self.map_free_gpu
                self.map_free_gpu = None
            cp.get_default_memory_pool().free_all_blocks()
            
            if self.map_free is not None:
                del self.map_free
                self.map_free = None
            
            if self.free_pixels is not None:
                del self.free_pixels
                self.free_pixels = None
            
            # Reset auto-localization state for new map
            self._auto_done = False
            # Re-enable auto-localize timer (restart the cancelled timer)
            if self._auto_localize_enabled and self._auto_timer:
                self._auto_timer.reset()
                self.get_logger().info('Re-enabled auto-localize timer for new map')
        else:
            self.get_logger().info(f'Received map: {msg.info.width}x{msg.info.height}, res={msg.info.resolution}')
        
        self._publish_status('processing_map')
        
        self.resolution = msg.info.resolution
        self.origin = [
            msg.info.origin.position.x,
            msg.info.origin.position.y,
            0.0
        ]
        
        # Convert data to numpy array
        # OccupancyGrid data is row-major, 0=bottom-left
        # -1: unknown, 0: free, 100: occupied
        data = np.array(msg.data, dtype=np.int8).reshape((msg.info.height, msg.info.width))
        
        # grid_localizer expects self.map_free where 1.0=free, 0.0=occupied
        # and row 0 = top of image (due to coordinate conversion logic)
        
        # 1. Create binary free map (0 is free in OccupancyGrid)
        # Treat unknown (-1) as occupied for safety
        map_free_binary = (data == 0).astype(np.float32)
        
        # 2. Flip vertically to match "image coordinates" expected by _generate_candidates_for_batch
        # (which uses map_h - pix_y)
        self.map_free = np.flipud(map_free_binary)
        
        self.map_h, self.map_w = self.map_free.shape
        
        # Precompute free space coordinates for candidate generation
        free_y, free_x = np.where(self.map_free > 0.5)
        # Subsample based on sample_distance
        step = max(1, int(self.sample_dist / self.resolution))
        mask = ((free_x % step) == 0) & ((free_y % step) == 0)
        self.free_pixels = np.stack([free_x[mask], free_y[mask]], axis=1)
        
        self.get_logger().info(f'Map processed: {len(self.free_pixels)} candidate positions')
        
        # Move map to GPU
        self.map_free_gpu = cp.asarray(self.map_free)
        
        # Record time map was received to allow for a startup delay
        self.map_received_time = self.get_clock().now()
        self.map_received = True

    def _scan_cb(self, msg: LaserScan):
        """Store latest scan."""
        self.latest_scan = msg

    def _auto_localize_tick(self):
        """Auto-localize on startup.
        
        Runs the full search in one blocking call for maximum speed.
        The search itself is GPU-bound and completes quickly (~1-2s).
        Retries on insufficient scan data, but gives up on other errors.
        """
        if self._auto_done:
            return
            
        # Need scan data and map before we start
        if not self.map_received:
            return

        if self.latest_scan is None:
            self.get_logger().info('Waiting for scan data...', throttle_duration_sec=2.0)
            return

        self.get_logger().info('Starting auto-localization search...')
        start_time = self.get_clock().now()
        
        try:
            pose, score = self._find_pose(self.latest_scan)
            elapsed = (self.get_clock().now() - start_time).nanoseconds / 1e9
            
            # Success - stop retrying
            self._auto_done = True
            if self._auto_timer:
                self._auto_timer.cancel()
            
            self._publish_pose(pose[0], pose[1], pose[2])
            
            if score < self.score_threshold:
                self._publish_status('localized')
                self.get_logger().info(
                    f'Auto-localized with high confidence at '
                    f'({pose[0]:.2f}, {pose[1]:.2f}, {np.degrees(pose[2]):.1f}°) '
                    f'score={score:.3f} in {elapsed:.2f}s'
                )
            else:
                self._publish_status('localized_low_confidence')
                self.get_logger().warn(
                    f'Auto-localized with LOW confidence at '
                    f'({pose[0]:.2f}, {pose[1]:.2f}, {np.degrees(pose[2]):.1f}°) '
                    f'score={score:.3f} (threshold: {self.score_threshold}) in {elapsed:.2f}s'
                )
        except ValueError as e:
            # Insufficient scan data - retry on next tick
            self.get_logger().warn(f'Scan insufficient, will retry: {e}', throttle_duration_sec=2.0)
        except Exception as e:
            # Other errors - give up
            self._auto_done = True
            if self._auto_timer:
                self._auto_timer.cancel()
            self._publish_status('error')
            self.get_logger().error(f'Auto-localization failed: {e}')

    def _process_scan(self, scan: LaserScan):
        """Extract and filter valid ranges from a laser scan.
        
        Returns:
            tuple: (ranges, angles) arrays of valid scan points, or (None, None) if insufficient data
        """
        ranges = np.array(scan.ranges, dtype=np.float32)
        angles = np.linspace(scan.angle_min, scan.angle_max, len(ranges), dtype=np.float32)
        
        # Filter valid ranges
        valid = (ranges > scan.range_min) & (ranges < min(scan.range_max, self.max_range))
        ranges = ranges[valid]
        angles = angles[valid]
        
        if len(ranges) < 10:
            return None, None
        
        return ranges, angles
    
    def _generate_candidates_for_batch(self, start_idx, end_idx):
        """Generate candidate poses for a specific batch range on-the-fly.
        
        This avoids storing all candidates in memory by computing them per-batch.
        Candidates are indexed as: idx = position_idx * n_angles + angle_idx
        
        Args:
            start_idx: Start index in the flattened candidate space
            end_idx: End index (exclusive) in the flattened candidate space
            
        Returns:
            tuple: (pos_x, pos_y, pos_theta) arrays for this batch only
        """
        n_ang = self.angle_samples
        angle_offsets = np.linspace(0, 2 * np.pi, n_ang, endpoint=False, dtype=np.float32)
        
        # Compute which positions and angles this batch covers
        batch_size = end_idx - start_idx
        batch_indices = np.arange(start_idx, end_idx, dtype=np.int32)
        
        # Decode position and angle indices
        pos_indices = batch_indices // n_ang
        ang_indices = batch_indices % n_ang
        
        # Get pixel coordinates for these positions
        pix_x = self.free_pixels[pos_indices, 0]
        pix_y = self.free_pixels[pos_indices, 1]
        
        # Convert to world coordinates
        pos_x = (pix_x * self.resolution + self.origin[0]).astype(np.float32)
        pos_y = ((self.map_h - pix_y) * self.resolution + self.origin[1]).astype(np.float32)
        pos_theta = angle_offsets[ang_indices]
        
        return pos_x, pos_y, pos_theta
    
    def _idx_to_pose(self, global_idx):
        """Convert a global candidate index back to (x, y, theta) pose.
        
        Args:
            global_idx: Index in the flattened candidate space
            
        Returns:
            tuple: (x, y, theta) world coordinates
        """
        n_ang = self.angle_samples
        angle_offsets = np.linspace(0, 2 * np.pi, n_ang, endpoint=False, dtype=np.float32)
        
        pos_idx = global_idx // n_ang
        ang_idx = global_idx % n_ang
        
        pix_x = self.free_pixels[pos_idx, 0]
        pix_y = self.free_pixels[pos_idx, 1]
        
        x = pix_x * self.resolution + self.origin[0]
        y = (self.map_h - pix_y) * self.resolution + self.origin[1]
        theta = angle_offsets[ang_idx]
        
        return float(x), float(y), float(theta)

    def _publish_status(self, status: str):
        """Publish status for app to consume."""
        if self.status_pub is None or not self.status_pub.is_activated:
            self.get_logger().warn(f'Status publisher is not active. Cannot publish status: {status}')
            return
        
        msg = String()
        msg.data = status
        self.status_pub.publish(msg)
        self.get_logger().info(f'Published status: {status}')

    def _publish_pose(self, x: float, y: float, theta: float):
        """Publish pose to /initialpose (latched for AMCL)."""
        if self.pose_pub is None or not self.pose_pub.is_activated:
            self.get_logger().warn('Pose publisher is not active. Cannot publish pose.')
            return
        
        msg = PoseWithCovarianceStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = 'map'
        msg.pose.pose.position.x = x
        msg.pose.pose.position.y = y
        msg.pose.pose.orientation.z = np.sin(theta / 2)
        msg.pose.pose.orientation.w = np.cos(theta / 2)
        msg.pose.covariance[0] = 0.1
        msg.pose.covariance[7] = 0.1
        msg.pose.covariance[35] = 0.05
        self.pose_pub.publish(msg)
        self.get_logger().info(f'Published initial pose: ({x:.2f}, {y:.2f})')

    def _localize_cb(self, request, response):
        """Service callback to trigger localization."""
        # Check if node is active
        if not self._is_active:
            response.success = False
            response.message = 'Node not active'
            return response
        
        if not self.map_received:
            response.success = False
            response.message = 'No map received yet'
            return response

        if self.latest_scan is None:
            response.success = False
            response.message = 'No scan received yet'
            return response
        
        try:
            pose, score = self._find_pose(self.latest_scan)
            
            self._publish_pose(pose[0], pose[1], pose[2])
            
            response.success = True
            response.message = f'Localized at ({pose[0]:.2f}, {pose[1]:.2f}, {np.degrees(pose[2]):.1f}°) score={score:.3f}'
            self.get_logger().info(response.message)
            
        except Exception as e:
            response.success = False
            response.message = str(e)
            self.get_logger().error(f'Localization failed: {e}')
        
        return response

    def _find_pose(self, scan: LaserScan) -> tuple:
        """Find best pose using GPU-accelerated scan matching with batched processing."""
        ranges, angles = self._process_scan(scan)
        
        if ranges is None:
            raise ValueError('Not enough valid scan points')
        
        n_positions = len(self.free_pixels)
        total = n_positions * self.angle_samples
        
        self.get_logger().info(f'Searching {n_positions} positions x {self.angle_samples} angles = {total} candidates')
        self.get_logger().info(f'Using {len(ranges)} scan rays')
        
        # Pre-upload scan data to GPU once (not per-batch)
        ranges_gpu = cp.asarray(ranges, dtype=cp.float32)
        angles_gpu = cp.asarray(angles, dtype=cp.float32)
        cos_angles = cp.cos(angles_gpu)
        sin_angles = cp.sin(angles_gpu)
        del angles_gpu
        
        # Process in batches to avoid OOM
        best_score = float('inf')
        best_idx = -1
        n_batches = (total + self.batch_size - 1) // self.batch_size
        
        for batch_num, start in enumerate(range(0, total, self.batch_size)):
            end = min(start + self.batch_size, total)
            
            # Generate candidates for this batch only (CPU side)
            batch_x, batch_y, batch_theta = self._generate_candidates_for_batch(start, end)
            
            # Score on GPU (returns CuPy array)
            scores_gpu = self._score_batch_gpu(batch_x, batch_y, batch_theta, ranges_gpu, cos_angles, sin_angles)
            
            # Find best in batch on GPU
            batch_best_idx = int(cp.argmin(scores_gpu))
            batch_best_score = float(scores_gpu[batch_best_idx])
            
            del scores_gpu
            
            if batch_best_score < best_score:
                best_score = batch_best_score
                best_idx = start + batch_best_idx
                self.get_logger().debug(f'Batch {batch_num+1}/{n_batches}: new best score {best_score:.4f}')
            
            # Log progress every 10 batches
            if (batch_num + 1) % 10 == 0 or (batch_num + 1) == n_batches:
                self.get_logger().info(f'Progress: {batch_num+1}/{n_batches} batches ({100*(batch_num+1)/n_batches:.0f}%)')
        
        # Cleanup scan data from GPU
        del ranges_gpu, cos_angles, sin_angles
        
        # Free GPU memory once at the end
        cp.get_default_memory_pool().free_all_blocks()
        
        best_pose = self._idx_to_pose(best_idx)
        return best_pose, best_score

    def _score_batch_gpu(self, pos_x, pos_y, pos_theta, ranges_gpu, cos_angles, sin_angles):
        """GPU-accelerated batch scoring with memory optimization.
        
        Args:
            pos_x, pos_y, pos_theta: numpy arrays of candidate poses
            ranges_gpu: CuPy array of scan ranges (pre-uploaded)
            cos_angles, sin_angles: CuPy arrays of precomputed trig (pre-uploaded)
        
        Returns:
            numpy array of scores (lower = better match)
        """
        # Move pose data to GPU
        pos_x_gpu = cp.asarray(pos_x, dtype=cp.float32)
        pos_y_gpu = cp.asarray(pos_y, dtype=cp.float32)
        pos_theta_gpu = cp.asarray(pos_theta, dtype=cp.float32)
        
        cos_theta = cp.cos(pos_theta_gpu)
        sin_theta = cp.sin(pos_theta_gpu)
        del pos_theta_gpu
        
        # Compute world angles using angle addition formula
        # cos(theta + angle) = cos(theta)*cos(angle) - sin(theta)*sin(angle)
        # sin(theta + angle) = sin(theta)*cos(angle) + cos(theta)*sin(angle)
        cos_world = cos_theta[:, None] * cos_angles[None, :] - sin_theta[:, None] * sin_angles[None, :]
        sin_world = sin_theta[:, None] * cos_angles[None, :] + cos_theta[:, None] * sin_angles[None, :]
        del cos_theta, sin_theta
        
        # Compute endpoint pixel coordinates
        # pix_x = (pos_x + range * cos_world - origin_x) / resolution
        inv_res = 1.0 / self.resolution
        
        pix_x = (pos_x_gpu[:, None] + ranges_gpu[None, :] * cos_world - self.origin[0]) * inv_res
        del cos_world
        cp.clip(pix_x, 0, self.map_w - 1, out=pix_x)
        
        pix_y = self.map_h - (pos_y_gpu[:, None] + ranges_gpu[None, :] * sin_world - self.origin[1]) * inv_res
        del sin_world, pos_x_gpu, pos_y_gpu
        cp.clip(pix_y, 0, self.map_h - 1, out=pix_y)
        
        # Convert to int for indexing
        pix_x_int = pix_x.astype(cp.int32)
        pix_y_int = pix_y.astype(cp.int32)
        del pix_x, pix_y
        
        # Score: lower = better (endpoints hitting obstacles = 0 in map_free)
        hit_free = self.map_free_gpu[pix_y_int, pix_x_int]
        del pix_x_int, pix_y_int
        
        # Mean across beams, keep on GPU
        scores = cp.mean(hit_free, axis=1)
        del hit_free
        
        return scores  # Return CuPy array, caller handles transfer


def main(args=None):
    rclpy.init(args=args)
    lc_node = GridLocalizer('grid_localizer')
    try:
        rclpy.spin(lc_node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        # Free GPU memory before exit so restarts don't OOM
        cp.get_default_memory_pool().free_all_blocks()
        cp.get_default_pinned_memory_pool().free_all_blocks()
        rclpy.shutdown()

if __name__ == '__main__':
    main()