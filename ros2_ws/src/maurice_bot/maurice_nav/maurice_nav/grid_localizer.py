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
import cv2

import cupy as cp


class GridLocalizer(Node):
    def __init__(self):
        super().__init__('grid_localizer')
        
        # Parameters
        self.declare_parameter('map_yaml_path', '')
        self.declare_parameter('sample_distance', 0.15)  # meters between samples
        self.declare_parameter('angle_samples', 36)  # angles to try (360/24 = 15° increments)
        self.declare_parameter('batch_size', 4000)  # poses per GPU batch (reduced for memory)
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
        self._search_initialized = False
        self._current_batch_idx = 0
        self._total_candidates = 0
        # Store only the position indices, not full expanded candidates
        self._n_positions = 0
        
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
        
        # Auto-localize timer (runs frequently to process batches)
        if auto_localize:
            self._auto_timer = self.create_timer(0.1, self._auto_localize_tick)
        
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

    def _auto_localize_tick(self):
        """Auto-localize on startup, searching all positions until complete or timeout."""
        if self._auto_done:
            return
        
        now = self.get_clock().now()
        
        # Need scan data before we start
        if self.latest_scan is None:
            self.get_logger().info('Waiting for scan data...')
            return
        
        # Initialize search on first tick WITH scan data
        if not self._search_initialized:
            self._auto_start_time = now
            self._initialize_search()
            return
        
        elapsed = (now - self._auto_start_time).nanoseconds / 1e9
        
        # Check if search is complete (all positions searched) or timeout
        search_complete = self._current_batch_idx >= self._total_candidates
        timed_out = elapsed > self.auto_timeout
        
        if search_complete or timed_out:
            self._auto_done = True
            
            if self._best_pose is not None:
                # Publish best pose found
                self._publish_pose(self._best_pose[0], self._best_pose[1], self._best_pose[2])
                
                # Use threshold to determine confidence level
                if self._best_score < self.score_threshold:
                    self._publish_status('localized')
                    self.get_logger().info(
                        f'Auto-localized with high confidence at '
                        f'({self._best_pose[0]:.2f}, {self._best_pose[1]:.2f}, {np.degrees(self._best_pose[2]):.1f}°) '
                        f'score={self._best_score:.3f} in {elapsed:.1f}s '
                        f'(searched {self._current_batch_idx}/{self._total_candidates} candidates)'
                    )
                else:
                    self._publish_status('localized_low_confidence')
                    self.get_logger().warn(
                        f'Auto-localized with low confidence at '
                        f'({self._best_pose[0]:.2f}, {self._best_pose[1]:.2f}, {np.degrees(self._best_pose[2]):.1f}°) '
                        f'score={self._best_score:.3f} (threshold: {self.score_threshold}) in {elapsed:.1f}s '
                        f'(searched {self._current_batch_idx}/{self._total_candidates} candidates)'
                    )
            else:
                self._publish_status('timeout')
                self.get_logger().error(f'Auto-localization timed out with no valid pose')
            return
        
        # Process next batch of candidates
        try:
            self._process_next_batch()
        except Exception as e:
            self.get_logger().warn(f'Batch processing failed: {e}')
    
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

    def _initialize_search(self):
        """Initialize the search by preparing scan data (candidates generated on-the-fly)."""
        ranges, angles = self._process_scan(self.latest_scan)
        
        if ranges is None:
            self.get_logger().warn('Not enough valid scan points, waiting for better scan...')
            return
        
        # Store processed scan for batch scoring
        self._scan_ranges = ranges
        self._scan_angles = angles
        
        # Calculate total candidates without generating them
        self._n_positions = len(self.free_pixels)
        self._total_candidates = self._n_positions * self.angle_samples
        
        self._current_batch_idx = 0
        self._search_initialized = True
        
        self.get_logger().info(
            f'Search initialized: {self._n_positions} positions x {self.angle_samples} angles = {self._total_candidates} candidates'
        )
        self.get_logger().info(f'Using {len(ranges)} scan rays')
    
    def _process_next_batch(self):
        """Process the next batch of candidate poses (generated on-the-fly)."""
        start = self._current_batch_idx
        end = min(start + self.batch_size, self._total_candidates)
        
        # Generate candidates for this batch only (saves memory)
        batch_x, batch_y, batch_theta = self._generate_candidates_for_batch(start, end)
        
        batch_scores = self._score_batch_gpu(
            batch_x,
            batch_y,
            batch_theta,
            self._scan_ranges,
            self._scan_angles
        )
        
        # Free batch arrays immediately
        del batch_x, batch_y, batch_theta
        
        batch_best_idx = np.argmin(batch_scores)
        batch_best_score = batch_scores[batch_best_idx]
        
        if batch_best_score < self._best_score:
            global_idx = start + batch_best_idx
            self._best_score = batch_best_score
            # Reconstruct pose from index (instead of storing all candidates)
            self._best_pose = self._idx_to_pose(global_idx)
            self.get_logger().info(
                f'New best pose: ({self._best_pose[0]:.2f}, {self._best_pose[1]:.2f}, '
                f'{np.degrees(self._best_pose[2]):.1f}°) score={self._best_score:.3f} '
                f'[batch {start//self.batch_size + 1}/{(self._total_candidates + self.batch_size - 1)//self.batch_size}]'
            )
        
        # Free scores array
        del batch_scores
        
        self._current_batch_idx = end
        
        # Log progress periodically
        progress = (end / self._total_candidates) * 100
        if end % (self.batch_size * 10) == 0 or end >= self._total_candidates:
            self.get_logger().info(f'Search progress: {progress:.1f}% ({end}/{self._total_candidates})')

    def _publish_status(self, status: str):
        """Publish status for app to consume."""
        msg = String()
        msg.data = status
        self.status_pub.publish(msg)
        self.get_logger().info(f'Published status: {status}')

    def _publish_pose(self, x: float, y: float, theta: float):
        """Publish pose to /initialpose (latched for AMCL)."""
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
        
        # Process in batches to avoid OOM
        best_score = float('inf')
        best_idx = -1
        
        for start in range(0, total, self.batch_size):
            end = min(start + self.batch_size, total)
            
            # Generate candidates for this batch only
            batch_x, batch_y, batch_theta = self._generate_candidates_for_batch(start, end)
            scores = self._score_batch_gpu(batch_x, batch_y, batch_theta, ranges, angles)
            
            # Free batch arrays
            del batch_x, batch_y, batch_theta
            
            batch_best_idx = np.argmin(scores)
            batch_best_score = scores[batch_best_idx]
            
            if batch_best_score < best_score:
                best_score = batch_best_score
                best_idx = start + batch_best_idx
            
            del scores
        
        best_pose = self._idx_to_pose(best_idx)
        return best_pose, best_score

    def _score_batch_gpu(self, pos_x, pos_y, pos_theta, ranges, angles):
        """GPU-accelerated batch scoring with memory optimization."""
        n_poses = len(pos_x)
        n_beams = len(ranges)
        
        # Move to GPU with explicit dtype to avoid copies
        pos_x_gpu = cp.asarray(pos_x, dtype=cp.float32)
        pos_y_gpu = cp.asarray(pos_y, dtype=cp.float32)
        pos_theta_gpu = cp.asarray(pos_theta, dtype=cp.float32)
        ranges_gpu = cp.asarray(ranges, dtype=cp.float32)
        angles_gpu = cp.asarray(angles, dtype=cp.float32)
        
        # Pre-compute cos/sin of angles (shared across all poses)
        cos_angles = cp.cos(angles_gpu)
        sin_angles = cp.sin(angles_gpu)
        del angles_gpu
        
        # Compute world angles and trig functions in-place
        # world_angles[i,j] = pos_theta[i] + angles[j]
        cos_world = cp.cos(pos_theta_gpu[:, None]) * cos_angles[None, :] - cp.sin(pos_theta_gpu[:, None]) * sin_angles[None, :]
        sin_world = cp.sin(pos_theta_gpu[:, None]) * cos_angles[None, :] + cp.cos(pos_theta_gpu[:, None]) * sin_angles[None, :]
        del pos_theta_gpu, cos_angles, sin_angles
        
        # Compute end points and convert to pixels in-place
        # pix_x = (pos_x + ranges * cos_world - origin_x) / resolution
        pix_x = pos_x_gpu[:, None] + ranges_gpu[None, :] * cos_world
        del cos_world
        pix_x -= self.origin[0]
        pix_x /= self.resolution
        cp.clip(pix_x, 0, self.map_w - 1, out=pix_x)
        pix_x = pix_x.astype(cp.int32)
        
        pix_y = pos_y_gpu[:, None] + ranges_gpu[None, :] * sin_world
        del sin_world, pos_x_gpu, pos_y_gpu, ranges_gpu
        pix_y -= self.origin[1]
        pix_y /= self.resolution
        pix_y = self.map_h - pix_y
        cp.clip(pix_y, 0, self.map_h - 1, out=pix_y)
        pix_y = pix_y.astype(cp.int32)
        
        # Score: lower = better (endpoints hitting obstacles)
        hit_free = self.map_free_gpu[pix_y, pix_x]
        del pix_x, pix_y
        
        scores = cp.mean(hit_free, axis=1)
        del hit_free
        
        result = cp.asnumpy(scores)
        del scores
        
        # Aggressively free GPU memory
        cp.get_default_memory_pool().free_all_blocks()
        cp.get_default_pinned_memory_pool().free_all_blocks()
        
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





