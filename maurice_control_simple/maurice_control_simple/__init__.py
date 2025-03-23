"""
Maurice Control Simple - A simplified interface for controlling Maurice robot.

This package provides interfaces for controlling both the arm (using Dynamixel)
and the wheels (using UART communication) of the Maurice robot.
"""

from maurice_control_simple.dynamixel import (
    Dynamixel,
    OperatingMode,
    ReadAttribute,
)
from maurice_control_simple.wheel_controller import WheelController

__all__ = [
    "Dynamixel",
    "OperatingMode",
    "ReadAttribute",
    "WheelController",
]
