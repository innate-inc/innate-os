# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""LeRobot plugin for the Innate MARS mobile manipulator.

Installed under the ``lerobot_robot_`` prefix so lerobot imports it at startup; importing
registers ``--robot.type=mars`` and ``--teleop.type=mars_passthrough``.
"""

from .config_mars import MarsConfig
from .config_mars_passthrough_teleop import MarsPassthroughTeleopConfig
from .mars import Mars
from .mars_passthrough_teleop import MarsPassthroughTeleop

__all__ = ["Mars", "MarsConfig", "MarsPassthroughTeleop", "MarsPassthroughTeleopConfig"]
