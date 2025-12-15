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
- Runs as a standalone node (not lifecycle-managed) to provide coarse
  localization before AMCL starts

On startup, automatically tries to localize for up to `auto_localize_timeout` seconds.
Publishes status to /localization/status ('localized' or 'timeout').
Service remains available for manual triggers after auto-localize completes.
"""

import os
import yaml
import numpy as np
import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
from sensor_msgs.msg import LaserScan
from geometry_msgs.msg import PoseWithCovarianceStamped
from std_srvs.srv import Trigger
from std_msgs.msg import String
from lifecycle_msgs.msg import TransitionEvent
import cv2

import cupy as cp


class GridLocalizer(Node):
    def __init__(self):
        super().__init__('grid_localizer')
        
        # Parameters
        self.declare_parameter('map_yaml_path', '')
        self.declare_parameter('sample_distance', 0.5)  # meters between samples
        self.declare_parameter('angle_samples', 24)  # angles to try (360/24 = 15° increments)
        self.declare_parameter('num_beams', 60)  # downsample scan to this many beams
        self.declare_parameter('batch_size', 8000)  # poses per GPU batch
        self.declare_parameter('max_range', 12.0)  # max lidar range
        self.declare_parameter('scan_topic', '/scan_fast')
        self.declare_parameter('auto_localize_timeout', 30.0)  # seconds
        self.declare_parameter('max_score_threshold', 0.3)  # lower = stricter match
        self.declare_parameter('auto_localize', True)  # enable auto-localize on startup
        
        map_yaml = self.get_parameter('map_yaml_path').value
        if not map_yaml:
            # Default path
            root = os.environ.get('INNATE_OS_ROOT', os.path.expanduser('~/innate-os'))
            map_yaml = os.path.join(root, 'maps', 'home.yaml')
        
        self.sample_dist = self.get_parameter('sample_distance').value
        self.angle_samples = self.get_parameter('angle_samples').value
        self.num_beams = self.get_parameter('num_beams').value
        self.max_range = self.get_parameter('max_range').value
        self.batch_size = self.get_parameter('batch_size').value
        scan_topic = self.get_parameter('scan_topic').value
        self.auto_timeout = self.get_parameter('auto_localize_timeout').value
        self.score_threshold = self.get_parameter('max_score_threshold').value
        auto_localize = self.get_parameter('auto_localize').value
        
        # Load map
        self._load_map(map_yaml)
        
        # Latest scan storage
        self.latest_scan = None
        
        # Auto-localize state
        self._auto_start_time = None
        self._auto_done = not auto_localize  # Skip if disabled
        self._best_pose = None
        self._best_score = float('inf')
        
        # Subscribers
        scan_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
        self.scan_sub = self.create_subscription(LaserScan, scan_topic, self._scan_cb, scan_qos)
        
        # Publishers
        # Latched publisher - message persists for late subscribers (AMCL)
        # This solves the race condition where grid_localizer publishes before AMCL starts
        latched_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            depth=1,
        )
        self.pose_pub = self.create_publisher(PoseWithCovarianceStamped, '/initialpose', latched_qos)
        self.status_pub = self.create_publisher(String, '/localization/status', 10)
        
        # Service (manual trigger, always available)
        self.srv = self.create_service(Trigger, 'localize', self._localize_cb)
        self.test_srv = self.create_service(Trigger, 'test', self._test_cb)
        
        # AMCL lifecycle state tracking
        self._amcl_active = False
        self._pending_pose = None  # Store pose to publish when AMCL becomes active
        self.create_subscription(
            TransitionEvent,
            '/amcl/transition_event',
            self._amcl_transition_cb,
            10
        )
        
        # Auto-localize timer (2 second intervals)
        if auto_localize:
            self._auto_timer = self.create_timer(2.0, self._auto_localize_tick)
        
        self.get_logger().info(f'Grid localizer ready. Map: {map_yaml}')
        if auto_localize:
            self.get_logger().info(f'Auto-localize enabled: {self.auto_timeout}s timeout, score threshold {self.score_threshold}')

    def _load_map(self, yaml_path: str):
        """Load occupancy grid map from yaml + image."""
        with open(yaml_path, 'r') as f:
            map_meta = yaml.safe_load(f)
        
        self.resolution = map_meta['resolution']
        self.origin = map_meta['origin']  # [x, y, theta]
        occupied_thresh = map_meta.get('occupied_thresh', 0.65)
        
        # Load image
        map_dir = os.path.dirname(yaml_path)
        img_path = os.path.join(map_dir, map_meta['image'])
        img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise ValueError(f'Failed to load map image: {img_path}')
        
        # Convert to binary: 1 = free, 0 = occupied/unknown
        # In standard maps: white (255) = free, black (0) = occupied
        if map_meta.get('negate', 0):
            img = 255 - img
        
        threshold = int((1.0 - occupied_thresh) * 255)
        self.map_free = (img > threshold).astype(np.float32)
        self.map_h, self.map_w = self.map_free.shape
        
        # Precompute free space coordinates for candidate generation
        free_y, free_x = np.where(self.map_free > 0.5)
        # Subsample based on sample_distance
        step = max(1, int(self.sample_dist / self.resolution))
        mask = ((free_x % step) == 0) & ((free_y % step) == 0)
        self.free_pixels = np.stack([free_x[mask], free_y[mask]], axis=1)
        
        self.get_logger().info(f'Map loaded: {self.map_w}x{self.map_h}, {len(self.free_pixels)} candidate positions')
        
        # Move map to GPU
        self.map_free_gpu = cp.asarray(self.map_free)

    def _scan_cb(self, msg: LaserScan):
        """Store latest scan."""
        self.latest_scan = msg

    def _amcl_transition_cb(self, msg: TransitionEvent):
        """Handle AMCL lifecycle transitions."""
        # State 3 = active
        if msg.goal_state.id == 3:
            self._amcl_active = True
            self.get_logger().info('AMCL is now active')
            # Publish any pending pose
            if self._pending_pose is not None:
                self.get_logger().info('Publishing pending initial pose to AMCL')
                self._do_publish_pose(self._pending_pose)
                self._pending_pose = None

    def _auto_localize_tick(self):
        """Auto-localize on startup until success or timeout."""
        if self._auto_done:
            return
        
        now = self.get_clock().now()
        
        # Need scan data before we start the timeout clock
        if self.latest_scan is None:
            self.get_logger().info('Waiting for scan data...')
            return
        
        # Initialize start time on first tick WITH scan data
        if self._auto_start_time is None:
            self._auto_start_time = now
            self.get_logger().info('Scan data received, starting auto-localization...')
            return
        
        elapsed = (now - self._auto_start_time).nanoseconds / 1e9
        
        # Check timeout
        if elapsed > self.auto_timeout:
            self._auto_done = True
            if self._best_pose is not None:
                # Use best attempt anyway - AMCL can refine it
                self._publish_pose(self._best_pose)
                self._publish_status('localized_low_confidence')
                self.get_logger().warn(
                    f'Timeout after {self.auto_timeout}s - using best guess: '
                    f'({self._best_pose[0]:.2f}, {self._best_pose[1]:.2f}) score={self._best_score:.3f}'
                )
            else:
                self._publish_status('timeout')
                self.get_logger().error(f'Auto-localization timed out with no valid pose')
            return
        
        # Try to localize
        try:
            pose, score = self._find_pose(self.latest_scan)
            
            # Track best pose seen so far
            if score < self._best_score:
                self._best_score = score
                self._best_pose = pose
            
            if score < self.score_threshold:
                # Success!
                self._publish_pose(pose)
                self._publish_status('localized')
                self._auto_done = True
                self.get_logger().info(
                    f'Auto-localized at ({pose[0]:.2f}, {pose[1]:.2f}, {np.degrees(pose[2]):.1f}°) '
                    f'score={score:.3f} in {elapsed:.1f}s'
                )
            else:
                self.get_logger().info(
                    f'Score {score:.3f} > {self.score_threshold}, retrying... '
                    f'(best so far: {self._best_score:.3f}, {elapsed:.0f}s)'
                )
                
        except Exception as e:
            self.get_logger().warn(f'Localization attempt failed: {e}')

    def _publish_status(self, status: str):
        """Publish status for app to consume."""
        msg = String()
        msg.data = status
        self.status_pub.publish(msg)
        self.get_logger().info(f'Published status: {status}')

    def _publish_pose(self, pose):
        """Publish pose to /initialpose (triggers AMCL). Queues if AMCL not active."""
        if self._amcl_active:
            self._do_publish_pose(pose)
        else:
            self.get_logger().info('AMCL not active yet, queuing pose for when it becomes active')
            self._pending_pose = pose

    def _do_publish_pose(self, pose):
        """Actually publish the pose message."""
        msg = PoseWithCovarianceStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = 'map'
        msg.pose.pose.position.x = pose[0]
        msg.pose.pose.position.y = pose[1]
        msg.pose.pose.orientation.z = np.sin(pose[2] / 2)
        msg.pose.pose.orientation.w = np.cos(pose[2] / 2)
        msg.pose.covariance[0] = 0.1
        msg.pose.covariance[7] = 0.1
        msg.pose.covariance[35] = 0.05
        self.pose_pub.publish(msg)
        self.get_logger().info(f'Published initial pose: ({pose[0]:.2f}, {pose[1]:.2f})')

    def _test_cb(self, request, response):
        """Service callback to trigger test."""
        response.success = True
        response.message = 'Test successful'
        return response

    def _localize_cb(self, request, response):
        """Service callback to trigger localization."""
        if self.latest_scan is None:
            response.success = False
            response.message = 'No scan received yet'
            return response
        
        try:
            pose, score = self._find_pose(self.latest_scan)
            
            # Publish to /initialpose
            pose_msg = PoseWithCovarianceStamped()
            pose_msg.header.stamp = self.get_clock().now().to_msg()
            pose_msg.header.frame_id = 'map'
            pose_msg.pose.pose.position.x = pose[0]
            pose_msg.pose.pose.position.y = pose[1]
            pose_msg.pose.pose.orientation.z = np.sin(pose[2] / 2)
            pose_msg.pose.pose.orientation.w = np.cos(pose[2] / 2)
            # Covariance (diagonal)
            pose_msg.pose.covariance[0] = 0.1  # x
            pose_msg.pose.covariance[7] = 0.1  # y
            pose_msg.pose.covariance[35] = 0.05  # yaw
            
            self.pose_pub.publish(pose_msg)
            
            response.success = True
            response.message = f'Localized at ({pose[0]:.2f}, {pose[1]:.2f}, {np.degrees(pose[2]):.1f}°) score={score:.3f}'
            self.get_logger().info(response.message)
            
        except Exception as e:
            response.success = False
            response.message = str(e)
            self.get_logger().error(f'Localization failed: {e}')
        
        return response

    def _find_pose(self, scan: LaserScan) -> tuple:
        """Find best pose using GPU-accelerated scan matching."""
        # Extract and downsample scan
        ranges = np.array(scan.ranges, dtype=np.float32)
        angles = np.linspace(scan.angle_min, scan.angle_max, len(ranges))
        
        # Filter valid ranges
        valid = (ranges > scan.range_min) & (ranges < min(scan.range_max, self.max_range))
        ranges = ranges[valid]
        angles = angles[valid]
        
        if len(ranges) < 10:
            raise ValueError('Not enough valid scan points')
        
        # Downsample to num_beams
        if len(ranges) > self.num_beams:
            idx = np.linspace(0, len(ranges) - 1, self.num_beams, dtype=int)
            ranges = ranges[idx]
            angles = angles[idx]
        
        # Generate candidate poses: (x, y, theta)
        angle_offsets = np.linspace(0, 2 * np.pi, self.angle_samples, endpoint=False)
        
        # Convert pixel coords to world coords
        candidates_x = self.free_pixels[:, 0] * self.resolution + self.origin[0]
        candidates_y = (self.map_h - self.free_pixels[:, 1]) * self.resolution + self.origin[1]
        
        n_pos = len(candidates_x)
        n_ang = len(angle_offsets)
        total = n_pos * n_ang
        
        self.get_logger().info(f'Searching {n_pos} positions x {n_ang} angles = {total} candidates')
        
        # Create full candidate array
        pos_x = np.repeat(candidates_x, n_ang)
        pos_y = np.repeat(candidates_y, n_ang)
        pos_theta = np.tile(angle_offsets, n_pos)
        
        # Process in batches to avoid OOM
        best_score = float('inf')
        best_idx = 0
        
        for start in range(0, total, self.batch_size):
            end = min(start + self.batch_size, total)
            
            batch_scores = self._score_batch_gpu(
                pos_x[start:end], pos_y[start:end], pos_theta[start:end],
                ranges, angles
            )
            
            batch_best_idx = np.argmin(batch_scores)
            if batch_scores[batch_best_idx] < best_score:
                best_score = batch_scores[batch_best_idx]
                best_idx = start + batch_best_idx
        
        best_pose = (pos_x[best_idx], pos_y[best_idx], pos_theta[best_idx])
        return best_pose, best_score

    def _score_batch_gpu(self, pos_x, pos_y, pos_theta, ranges, angles):
        """GPU-accelerated batch scoring."""
        # Move to GPU
        pos_x_gpu = cp.asarray(pos_x, dtype=cp.float32)
        pos_y_gpu = cp.asarray(pos_y, dtype=cp.float32)
        pos_theta_gpu = cp.asarray(pos_theta, dtype=cp.float32)
        ranges_gpu = cp.asarray(ranges, dtype=cp.float32)
        angles_gpu = cp.asarray(angles, dtype=cp.float32)
        
        # World angles: (n_poses, n_beams)
        world_angles = pos_theta_gpu[:, None] + angles_gpu[None, :]
        
        # Ray end points
        end_x = pos_x_gpu[:, None] + ranges_gpu[None, :] * cp.cos(world_angles)
        end_y = pos_y_gpu[:, None] + ranges_gpu[None, :] * cp.sin(world_angles)
        del world_angles  # Free intermediate
        
        # Convert to pixel coordinates
        pix_x = ((end_x - self.origin[0]) / self.resolution).astype(cp.int32)
        pix_y = (self.map_h - (end_y - self.origin[1]) / self.resolution).astype(cp.int32)
        del end_x, end_y
        
        # Clamp to map bounds
        pix_x = cp.clip(pix_x, 0, self.map_w - 1)
        pix_y = cp.clip(pix_y, 0, self.map_h - 1)
        
        # Score: lower = better (endpoints hitting obstacles)
        hit_free = self.map_free_gpu[pix_y, pix_x]
        scores = cp.mean(hit_free, axis=1)
        result = cp.asnumpy(scores)
        
        # Free GPU memory
        del pix_x, pix_y, hit_free, scores
        cp.get_default_memory_pool().free_all_blocks()
        
        return result

def main(args=None):
    rclpy.init(args=args)
    node = GridLocalizer()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()





