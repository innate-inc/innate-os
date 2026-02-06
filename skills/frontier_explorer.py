#!/usr/bin/env python3
"""
Frontier Exploration Skill - Wavefront frontier exploration for autonomous mapping.

This skill detects frontiers (boundaries between known and unknown space) and
navigates to them to expand the map. Requires the robot to be in mapping mode
where slam_toolbox is actively updating the map.

Based on wavefront frontier exploration algorithm.
"""

import base64
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import IntFlag
from typing import Optional

import numpy as np
from geometry_msgs.msg import PoseStamped
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from std_msgs.msg import String

from brain_client.skill_types import Skill, SkillResult, RobotStateType


class PointClassification(IntFlag):
    """Point classification flags for frontier detection algorithm."""
    NoInformation = 0
    MapOpen = 1
    MapClosed = 2
    FrontierOpen = 4
    FrontierClosed = 8


@dataclass
class GridPoint:
    """Represents a point in the grid map with classification."""
    x: int
    y: int
    classification: int = PointClassification.NoInformation


class FrontierCache:
    """Cache for grid points to avoid duplicate point creation."""

    def __init__(self):
        self.points = {}

    def get_point(self, x: int, y: int) -> GridPoint:
        """Get or create a grid point at the given coordinates."""
        key = (x, y)
        if key not in self.points:
            self.points[key] = GridPoint(x, y)
        return self.points[key]

    def clear(self):
        """Clear the point cache."""
        self.points.clear()


class FrontierExplorer(Skill):
    """
    Wavefront frontier exploration skill for autonomous mapping.
    
    Detects frontiers in the occupancy grid and navigates to them to expand
    the mapped area. Must be run in mapping mode.
    """
    
    # Occupancy grid values (ROS2 standard)
    UNKNOWN = -1
    FREE = 0
    OCCUPIED_THRESHOLD = 50  # Cells with value >= this are obstacles
    
    def __init__(self, logger):
        super().__init__(logger)
        self.navigator = BasicNavigator()
        self._cancel_requested = threading.Event()
        self._cache = FrontierCache()
        
        # Configuration parameters
        self.min_frontier_size = 5  # Minimum cells to consider a valid frontier
        self.safe_distance = 0.5  # Safe distance from obstacles (meters)
        self.goal_timeout = 60.0  # Timeout for reaching a goal (seconds)
        self.max_consecutive_failures = 5  # Max failures before giving up
        
        # State tracking
        self.explored_goals = []
        self.last_map_info_count = 0
        
        # Map data (updated via update_robot_state)
        self._map_data = None
        self._odom_data = None
        
        # Mode subscription
        self._current_mode = None
        self._mode_sub = None
    
    @property
    def name(self):
        return "frontier_explorer"
    
    @property
    def metadata(self):
        return {
            "description": (
                "Autonomous frontier exploration for mapping. Finds unexplored areas "
                "and navigates to them to expand the map. Must be in mapping mode."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_goals": {
                        "type": "integer",
                        "description": "Maximum number of frontier goals to visit (default: 20)"
                    },
                    "min_frontier_size": {
                        "type": "integer",
                        "description": "Minimum frontier size in cells to consider valid (default: 5)"
                    }
                },
                "required": []
            }
        }
    
    def guidelines(self):
        return (
            "Use for autonomous exploration to build a map. The robot must be in "
            "mapping mode (slam_toolbox running). The skill will find unexplored "
            "areas (frontiers) and navigate to them until no more frontiers exist "
            "or max_goals is reached."
        )
    
    def get_required_robot_states(self) -> list[RobotStateType]:
        return [RobotStateType.LAST_MAP, RobotStateType.LAST_ODOM]
    
    def update_robot_state(self, **kwargs):
        """Receive continuous map and odometry updates."""
        if RobotStateType.LAST_MAP.value in kwargs:
            self._map_data = kwargs[RobotStateType.LAST_MAP.value]
        if RobotStateType.LAST_ODOM.value in kwargs:
            self._odom_data = kwargs[RobotStateType.LAST_ODOM.value]
    
    def _check_mapping_mode(self) -> bool:
        """Check if we're in mapping mode by subscribing to /nav/current_mode."""
        if self.node is None:
            self.logger.warn("No ROS node available to check mode")
            return True  # Proceed anyway
        
        mode_received = threading.Event()
        current_mode = [None]
        
        def mode_callback(msg):
            current_mode[0] = msg.data
            mode_received.set()
        
        sub = self.node.create_subscription(String, '/nav/current_mode', mode_callback, 10)
        
        # Wait up to 2 seconds for mode message
        if mode_received.wait(timeout=2.0):
            self.node.destroy_subscription(sub)
            if current_mode[0] != "mapping":
                self.logger.error(f"Not in mapping mode (current: {current_mode[0]}). "
                                  "Frontier exploration requires mapping mode.")
                return False
            self.logger.info("Confirmed: Robot is in mapping mode")
            return True
        else:
            self.node.destroy_subscription(sub)
            self.logger.warn("Could not verify navigation mode, proceeding anyway")
            return True
    
    def _decode_map(self, map_data: dict) -> tuple:
        """
        Decode map data from the injected robot state.
        
        Returns:
            tuple: (grid, resolution, origin_x, origin_y, width, height) or None if invalid
        """
        if map_data is None:
            return None
        
        try:
            info = map_data["info"]
            width = info["width"]
            height = info["height"]
            resolution = info["resolution"]
            origin_x = info["origin"]["position"]["x"]
            origin_y = info["origin"]["position"]["y"]
            
            # Decode grid data
            grid_bytes = base64.b64decode(map_data["data_b64"])
            grid = np.frombuffer(grid_bytes, dtype=np.int8).reshape(height, width)
            
            return grid, resolution, origin_x, origin_y, width, height
        except Exception as e:
            self.logger.error(f"Failed to decode map: {e}")
            return None
    
    def _get_robot_pose(self) -> tuple:
        """Get robot position from odometry data."""
        if self._odom_data is None:
            return None
        
        try:
            pose = self._odom_data["pose"]["pose"]
            x = pose["position"]["x"]
            y = pose["position"]["y"]
            return x, y
        except Exception as e:
            self.logger.error(f"Failed to get robot pose: {e}")
            return None
    
    def _world_to_grid(self, wx: float, wy: float, resolution: float, 
                       origin_x: float, origin_y: float) -> tuple:
        """Convert world coordinates to grid coordinates."""
        gx = int((wx - origin_x) / resolution)
        gy = int((wy - origin_y) / resolution)
        return gx, gy
    
    def _grid_to_world(self, gx: int, gy: int, resolution: float,
                       origin_x: float, origin_y: float) -> tuple:
        """Convert grid coordinates to world coordinates."""
        wx = gx * resolution + origin_x + resolution / 2
        wy = gy * resolution + origin_y + resolution / 2
        return wx, wy
    
    def _get_neighbors(self, point: GridPoint, width: int, height: int) -> list:
        """Get valid 8-connected neighboring points."""
        neighbors = []
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                if dx == 0 and dy == 0:
                    continue
                nx, ny = point.x + dx, point.y + dy
                if 0 <= nx < width and 0 <= ny < height:
                    neighbors.append(self._cache.get_point(nx, ny))
        return neighbors
    
    def _is_frontier_point(self, point: GridPoint, grid: np.ndarray, 
                           width: int, height: int) -> bool:
        """
        Check if a point is a frontier point.
        A frontier point is an unknown cell adjacent to at least one free cell
        and not adjacent to any occupied cells.
        """
        # Point must be unknown
        if grid[point.y, point.x] != self.UNKNOWN:
            return False
        
        has_free = False
        for neighbor in self._get_neighbors(point, width, height):
            neighbor_val = grid[neighbor.y, neighbor.x]
            
            # If adjacent to occupied space, not a frontier
            if neighbor_val >= self.OCCUPIED_THRESHOLD:
                return False
            
            # Check if adjacent to free space
            if neighbor_val == self.FREE:
                has_free = True
        
        return has_free
    
    def _find_free_space(self, start_x: int, start_y: int, grid: np.ndarray,
                         width: int, height: int) -> tuple:
        """Find nearest free space using BFS from starting position."""
        queue = deque([self._cache.get_point(start_x, start_y)])
        visited = set()
        
        while queue:
            point = queue.popleft()
            if (point.x, point.y) in visited:
                continue
            visited.add((point.x, point.y))
            
            if grid[point.y, point.x] == self.FREE:
                return point.x, point.y
            
            for neighbor in self._get_neighbors(point, width, height):
                if (neighbor.x, neighbor.y) not in visited:
                    queue.append(neighbor)
        
        return start_x, start_y
    
    def _detect_frontiers(self, robot_x: float, robot_y: float, 
                          grid: np.ndarray, resolution: float,
                          origin_x: float, origin_y: float,
                          width: int, height: int) -> list:
        """
        Main frontier detection using wavefront exploration.
        
        Returns:
            List of (world_x, world_y, size) tuples for each frontier centroid
        """
        self._cache.clear()
        
        # Convert robot pose to grid coordinates
        grid_x, grid_y = self._world_to_grid(robot_x, robot_y, resolution, origin_x, origin_y)
        
        # Clamp to grid bounds
        grid_x = max(0, min(grid_x, width - 1))
        grid_y = max(0, min(grid_y, height - 1))
        
        # Find nearest free space to start exploration
        free_x, free_y = self._find_free_space(grid_x, grid_y, grid, width, height)
        start_point = self._cache.get_point(free_x, free_y)
        start_point.classification = PointClassification.MapOpen
        
        map_queue = deque([start_point])
        frontiers = []
        
        while map_queue:
            current = map_queue.popleft()
            
            if current.classification & PointClassification.MapClosed:
                continue
            
            current.classification |= PointClassification.MapClosed
            
            # Check if this point starts a new frontier
            if self._is_frontier_point(current, grid, width, height):
                current.classification |= PointClassification.FrontierOpen
                frontier_queue = deque([current])
                new_frontier = []
                
                # BFS to find all connected frontier points
                while frontier_queue:
                    fp = frontier_queue.popleft()
                    
                    if fp.classification & PointClassification.FrontierClosed:
                        continue
                    
                    if self._is_frontier_point(fp, grid, width, height):
                        new_frontier.append(fp)
                        
                        for neighbor in self._get_neighbors(fp, width, height):
                            if not (neighbor.classification & 
                                    (PointClassification.FrontierOpen | 
                                     PointClassification.FrontierClosed)):
                                neighbor.classification |= PointClassification.FrontierOpen
                                frontier_queue.append(neighbor)
                    
                    fp.classification |= PointClassification.FrontierClosed
                
                # Check if frontier is large enough
                if len(new_frontier) >= self.min_frontier_size:
                    # Compute centroid in world coordinates
                    cx = sum(p.x for p in new_frontier) / len(new_frontier)
                    cy = sum(p.y for p in new_frontier) / len(new_frontier)
                    wx, wy = self._grid_to_world(int(cx), int(cy), resolution, origin_x, origin_y)
                    frontiers.append((wx, wy, len(new_frontier)))
            
            # Add neighbors to exploration queue
            for neighbor in self._get_neighbors(current, width, height):
                if not (neighbor.classification & 
                        (PointClassification.MapOpen | PointClassification.MapClosed)):
                    cell_val = grid[neighbor.y, neighbor.x]
                    if cell_val == self.FREE or cell_val == self.UNKNOWN:
                        neighbor.classification |= PointClassification.MapOpen
                        map_queue.append(neighbor)
        
        return frontiers
    
    def _distance_to_obstacles(self, gx: int, gy: int, grid: np.ndarray,
                               resolution: float, width: int, height: int) -> float:
        """Compute distance to nearest obstacle from grid point."""
        search_radius = int(self.safe_distance / resolution) + 2
        min_dist = float('inf')
        
        for dy in range(-search_radius, search_radius + 1):
            for dx in range(-search_radius, search_radius + 1):
                cx, cy = gx + dx, gy + dy
                if 0 <= cx < width and 0 <= cy < height:
                    if grid[cy, cx] >= self.OCCUPIED_THRESHOLD:
                        dist = math.sqrt(dx**2 + dy**2) * resolution
                        min_dist = min(min_dist, dist)
        
        return min_dist if min_dist != float('inf') else self.safe_distance
    
    def _rank_frontiers(self, frontiers: list, robot_x: float, robot_y: float,
                        grid: np.ndarray, resolution: float,
                        origin_x: float, origin_y: float,
                        width: int, height: int) -> list:
        """
        Rank frontiers by score considering distance, size, and safety.
        
        Returns:
            Sorted list of (world_x, world_y, score) tuples
        """
        if not frontiers:
            return []
        
        scored = []
        for wx, wy, size in frontiers:
            # Distance from robot
            dist = math.sqrt((wx - robot_x)**2 + (wy - robot_y)**2)
            
            # Prefer moderate distances (not too close, not too far)
            dist_score = 1.0 / (1.0 + abs(dist - 3.0))  # Prefer ~3m away
            
            # Size score (larger frontiers = more information)
            size_score = min(size / 50.0, 1.0)
            
            # Safety score (distance from obstacles)
            gx, gy = self._world_to_grid(wx, wy, resolution, origin_x, origin_y)
            gx = max(0, min(gx, width - 1))
            gy = max(0, min(gy, height - 1))
            obs_dist = self._distance_to_obstacles(gx, gy, grid, resolution, width, height)
            safety_score = min(obs_dist / self.safe_distance, 1.0)
            
            # Distance from previously explored goals
            min_explored_dist = float('inf')
            for ex, ey in self.explored_goals:
                ed = math.sqrt((wx - ex)**2 + (wy - ey)**2)
                min_explored_dist = min(min_explored_dist, ed)
            novelty_score = min(min_explored_dist / 5.0, 1.0) if self.explored_goals else 1.0
            
            # Combined score
            total_score = (
                0.25 * dist_score +
                0.25 * size_score +
                0.25 * safety_score +
                0.25 * novelty_score
            )
            
            # Filter out unsafe goals
            if safety_score < 0.3:
                continue
            
            scored.append((wx, wy, total_score))
        
        # Sort by score descending
        scored.sort(key=lambda x: x[2], reverse=True)
        return scored
    
    def _count_map_info(self, grid: np.ndarray) -> int:
        """Count known cells (free + occupied) in the map."""
        free = np.sum(grid == self.FREE)
        occupied = np.sum(grid >= self.OCCUPIED_THRESHOLD)
        return int(free + occupied)
    
    def _navigate_to_goal(self, x: float, y: float) -> TaskResult:
        """Navigate to a goal position using Nav2."""
        self._cancel_requested.clear()
        
        goal = PoseStamped()
        goal.header.frame_id = "map"
        goal.header.stamp = self.navigator.get_clock().now().to_msg()
        goal.pose.position.x = x
        goal.pose.position.y = y
        goal.pose.position.z = 0.0
        goal.pose.orientation.w = 1.0
        
        self.logger.info(f"Navigating to frontier at ({x:.2f}, {y:.2f})")
        self.navigator.goToPose(goal)
        
        start_time = time.time()
        while not self.navigator.isTaskComplete():
            if self._cancel_requested.is_set():
                self.navigator.cancelTask()
                return TaskResult.CANCELED
            
            if time.time() - start_time > self.goal_timeout:
                self.logger.warn("Goal timeout, canceling")
                self.navigator.cancelTask()
                return TaskResult.FAILED
            
            time.sleep(0.1)
        
        return self.navigator.getResult()
    
    def execute(self, max_goals: int = 20, min_frontier_size: int = 5):
        """
        Execute frontier exploration.
        
        Args:
            max_goals: Maximum number of frontiers to visit
            min_frontier_size: Minimum frontier size in cells
        
        Returns:
            Tuple of (message, SkillResult)
        """
        self._cancel_requested.clear()
        self.min_frontier_size = min_frontier_size
        self.explored_goals = []
        
        # Verify mapping mode
        if not self._check_mapping_mode():
            return "Not in mapping mode. Switch to mapping mode first.", SkillResult.FAILURE
        
        # Wait for initial map data
        wait_start = time.time()
        while self._map_data is None or self._odom_data is None:
            if time.time() - wait_start > 10.0:
                return "Timeout waiting for map/odometry data", SkillResult.FAILURE
            if self._cancel_requested.is_set():
                return "Exploration cancelled", SkillResult.CANCELLED
            time.sleep(0.5)
        
        self.logger.info(f"Starting frontier exploration (max_goals={max_goals})")
        self._send_feedback("Starting frontier exploration...")
        
        goals_reached = 0
        consecutive_failures = 0
        
        while goals_reached < max_goals and not self._cancel_requested.is_set():
            # Decode current map
            map_result = self._decode_map(self._map_data)
            if map_result is None:
                self.logger.warn("Failed to decode map, retrying...")
                time.sleep(1.0)
                continue
            
            grid, resolution, origin_x, origin_y, width, height = map_result
            
            # Get robot position
            robot_pose = self._get_robot_pose()
            if robot_pose is None:
                self.logger.warn("Failed to get robot pose, retrying...")
                time.sleep(1.0)
                continue
            
            robot_x, robot_y = robot_pose
            
            # Detect frontiers
            frontiers = self._detect_frontiers(
                robot_x, robot_y, grid, resolution, origin_x, origin_y, width, height
            )
            
            if not frontiers:
                self.logger.info("No frontiers found - exploration complete!")
                self._send_feedback("Exploration complete - no more frontiers")
                break
            
            # Rank frontiers
            ranked = self._rank_frontiers(
                frontiers, robot_x, robot_y, grid, resolution, 
                origin_x, origin_y, width, height
            )
            
            if not ranked:
                self.logger.info("No safe frontiers found - exploration complete!")
                self._send_feedback("Exploration complete - no safe frontiers")
                break
            
            # Navigate to best frontier
            goal_x, goal_y, score = ranked[0]
            self._send_feedback(f"Navigating to frontier {goals_reached + 1}/{max_goals}")
            
            result = self._navigate_to_goal(goal_x, goal_y)
            
            if result == TaskResult.SUCCEEDED:
                goals_reached += 1
                consecutive_failures = 0
                self.explored_goals.append((goal_x, goal_y))
                self.logger.info(f"Reached frontier {goals_reached}/{max_goals}")
                
                # Check information gain
                new_info = self._count_map_info(grid)
                if self.last_map_info_count > 0:
                    gain = (new_info - self.last_map_info_count) / self.last_map_info_count
                    self.logger.info(f"Map information gain: {gain*100:.1f}%")
                self.last_map_info_count = new_info
                
            elif result == TaskResult.CANCELED:
                return "Exploration cancelled", SkillResult.CANCELLED
            else:
                consecutive_failures += 1
                self.logger.warn(f"Failed to reach frontier (attempt {consecutive_failures})")
                # Mark goal as explored anyway to avoid retrying
                self.explored_goals.append((goal_x, goal_y))
                
                if consecutive_failures >= self.max_consecutive_failures:
                    self.logger.warn("Too many consecutive failures")
                    break
            
            # Small delay before next iteration
            time.sleep(0.5)
        
        if self._cancel_requested.is_set():
            return "Exploration cancelled", SkillResult.CANCELLED
        
        return f"Exploration complete. Visited {goals_reached} frontiers.", SkillResult.SUCCESS
    
    def cancel(self):
        """Cancel the exploration."""
        self._cancel_requested.set()
        try:
            self.navigator.cancelTask()
        except:
            pass
        return "Frontier exploration cancelled"
