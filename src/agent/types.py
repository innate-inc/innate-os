from typing import NamedTuple
import numpy as np


#
# Typed “interfaces” for messages we put in or get out of queues
#
class ImageMsg(NamedTuple):
    """Simulation -> Agent: an RGB (and optionally depth) frame."""

    rgb_frame: np.ndarray
    depth_frame: np.ndarray | None


class VelocityCmd(NamedTuple):
    """Agent -> Simulation: a velocity command (linear_x, angular_z)."""

    linear_x: float
    angular_z: float


class CommentMsg(NamedTuple):
    """A textual comment or note from /comment_bell."""

    text: str
