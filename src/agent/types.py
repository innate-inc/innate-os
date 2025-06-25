# intypes.py

from typing import NamedTuple, Dict, Any, List
import numpy as np
import time


class RobotStateMsg(NamedTuple):
    """
    A single 'snapshot' of the robot's current sensor data & odometry.

    - rgb_frame, depth_frame: camera images (numpy arrays)
    - width, height, fx, fy, cx, cy, etc.: camera intrinsics
    - px,py,pz + ox,oy,oz,ow: robot pose in the world (position + quaternion)
    - vx,vy,vz + wx,wy,wz: robot linear and angular velocities
    """

    # --- camera images ---
    rgb_frame: np.ndarray
    depth_frame: np.ndarray | None

    # --- camera intrinsics ---
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    frame_id: str = "camera_color_frame"
    distortion_model: str = "plumb_bob"
    D: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0]

    # --- odometry: pose in world ---
    px: float = 0.0
    py: float = 0.0
    pz: float = 0.0
    ox: float = 0.0
    oy: float = 0.0
    oz: float = 0.0
    ow: float = 1.0

    # --- odometry: velocities ---
    vx: float = 0.0
    vy: float = 0.0
    vz: float = 0.0
    wx: float = 0.0
    wy: float = 0.0
    wz: float = 0.0


class OccupancyGridMsg(NamedTuple):
    """
    A map/occupancy grid, published only every N steps or seconds.
    Typically: 0=free, 100=occupied, -1=unknown in data.
    """

    width: int
    height: int
    resolution: float  # meters per cell
    origin_x: float
    origin_y: float
    origin_z: float
    origin_yaw: float
    data: np.ndarray  # 2D or 1D array of int8 in [-1..100]
    frame_id: str = "map"


class VelocityCmd(NamedTuple):
    """Agent -> Simulation: a velocity command (linear_x, angular_z)."""

    linear_x: float
    angular_z: float


class PositionCmd(NamedTuple):
    """Agent -> Simulation: a direct position command for navigation."""

    target_x: float
    target_y: float
    target_z: float = 0.0
    target_yaw: float = 0.0  # Optional orientation


class ResetRobotCmd:
    """
    A command telling the simulator to reset the robot's pose.
    Optionally includes memory state information to be loaded by the robot.
    Optionally includes pose (position and orientation) to reset the robot to.
    """

    def __init__(self, memory_state: str = None, pose: tuple = None):
        """
        Initialize a reset command with optional memory state and pose information.

        Args:
            memory_state: Identifier for the memory state to load,
                         e.g., "init_mem_human_rescue_and_email_test"
            pose: Tuple containing (position, orientation) where:
                 - position is a tuple of (x, y, z) coordinates
                 - orientation is a tuple of (w, x, y, z) quaternion values
                 If None, the default position and orientation will be used.
        """
        self.memory_state = memory_state
        self.pose = pose


class BrainActiveCmd(NamedTuple):
    """
    A command to activate or deactivate the brain.
    """

    active: bool


class DirectiveCmd(NamedTuple):
    """
    A command to update the robot's directive/behavior.
    """

    directive: str


class SetEnvironmentCmd(NamedTuple):
    """Command to set the simulation environment from a configuration dict."""

    config: Dict[str, Any]
    timestamp: float = time.time()


# Navigation-related message types
class NavigationWaypoint(NamedTuple):
    """A single waypoint in a navigation path."""

    x: float
    y: float
    yaw: float


class NavigationPathMsg(NamedTuple):
    """
    Navigation path from brain client.
    Represents a nav_msgs/Path message.
    """

    frame_id: str
    waypoints: List[NavigationWaypoint]  # Complete path from Nav2


class NavigationCancelMsg(NamedTuple):
    """
    Navigation cancellation request from brain client.
    Represents a Bool message.
    """

    cancel: bool


class NavigationStatusMsg(NamedTuple):
    """
    Navigation status to be published to brain client.
    Represents a String message.
    """

    status: str  # "IDLE", "ACTIVE", "SUCCEEDED", "FAILED", "CANCELED"


class NavigationFeedbackMsg(NamedTuple):
    """
    Navigation feedback to be published to brain client.
    Represents a Point message.
    """

    distance_to_goal: float  # x field
    unused_y: float = 0.0  # y field
    unused_z: float = 0.0  # z field
