from typing import NamedTuple
import numpy as np


#
# Typed “interfaces” for messages we put in or get out of queues
#
class ImageMsg(NamedTuple):
    """Simulation -> Agent: an RGB (and optionally depth) frame."""

    rgb_frame: np.ndarray
    depth_frame: np.ndarray | None


class CameraInfoMsg(NamedTuple):
    """Simulation -> Agent: camera info (intrinsics, distortion params, etc.)"""

    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    frame_id: str = "camera_color_frame"
    # If you want to store depth intrinsics separately or do a single set,
    # you can add more fields or define a second message for depth.
    # Distortion model & coefficients if needed
    distortion_model: str = "plumb_bob"
    D: list[float] = None  # e.g., [0, 0, 0, 0, 0]


class VelocityCmd(NamedTuple):
    """Agent -> Simulation: a velocity command (linear_x, angular_z)."""

    linear_x: float
    angular_z: float


class CommentMsg(NamedTuple):
    """A textual comment or note from /comment_bell."""

    text: str
