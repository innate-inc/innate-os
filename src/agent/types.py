# intypes.py

from typing import NamedTuple
import numpy as np


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


class ResetRobotCmd:
    """
    A command telling the simulator to reset the robot's pose to the origin.
    Optionally includes memory state information to be loaded by the robot.
    """

    def __init__(self, memory_state: str = None):
        """
        Initialize a reset command with optional memory state information.

        Args:
            memory_state: Identifier for the memory state to load,
                         e.g., "init_mem_human_rescue_and_email_test"
        """
        self.memory_state = memory_state


class DirectiveCmd(NamedTuple):
    """
    A command to update the robot's directive/behavior.
    """

    directive: str
