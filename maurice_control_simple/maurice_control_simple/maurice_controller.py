"""
Main controller for Maurice robot.

This module provides a simple interface for controlling Maurice's movement and arm
without ROS dependencies.
"""

import numpy as np
import time
from typing import List, Union, Optional

from .dynamixel import Dynamixel, OperatingMode
from .robot import Robot


class MauriceController:
    """
    Simple controller for Maurice robot.

    This class provides methods for controlling Maurice's movement and arm
    without ROS dependencies.
    """

    def __init__(
        self,
        device_name: str = "/dev/ttyACM0",
        baudrate: int = 1000000,
        arm_servo_ids: List[int] = None,
        wheel_servo_ids: List[int] = None,
    ):
        """
        Initialize the Maurice controller.

        Args:
            device_name: Serial port device name
            baudrate: Serial port baud rate
            arm_servo_ids: List of servo IDs for the arm (default: [1, 2, 3, 4, 5])
            wheel_servo_ids: List of servo IDs for the wheels (default: [10, 11])
        """
        self.device_name = device_name
        self.baudrate = baudrate
        self.arm_servo_ids = arm_servo_ids or [1, 2, 3, 4, 5]
        self.wheel_servo_ids = wheel_servo_ids or [10, 11]

        # Initialize Dynamixel interface
        self.dynamixel = Dynamixel(device_name=device_name, baudrate=baudrate)

        # Initialize arm robot
        self.arm = Robot(dynamixel=self.dynamixel, servo_ids=self.arm_servo_ids)

        # Configure wheel servos
        self._configure_wheels()

        print(f"Maurice controller initialized on {device_name}")

    def _configure_wheels(self):
        """Configure the wheel servos for velocity control."""
        # Disable torque to change operating mode
        for motor_id in self.wheel_servo_ids:
            self.dynamixel._disable_torque(motor_id)
            self.dynamixel.set_operating_mode(motor_id, OperatingMode.VELOCITY)
            self.dynamixel._enable_torque(motor_id)

    def move_forward(self, speed: float = 0.5):
        """
        Move the robot forward.

        Args:
            speed: Speed from 0.0 to 1.0 (default: 0.5)
        """
        if not 0.0 <= speed <= 1.0:
            raise ValueError("Speed must be between 0.0 and 1.0")

        # Convert speed to PWM values (0-885)
        pwm_value = int(speed * 885)

        # Set PWM values for wheels (one positive, one negative for forward motion)
        self._set_wheel_pwm(pwm_value, -pwm_value)

    def move_backward(self, speed: float = 0.5):
        """
        Move the robot backward.

        Args:
            speed: Speed from 0.0 to 1.0 (default: 0.5)
        """
        if not 0.0 <= speed <= 1.0:
            raise ValueError("Speed must be between 0.0 and 1.0")

        # Convert speed to PWM values (0-885)
        pwm_value = int(speed * 885)

        # Set PWM values for wheels (one negative, one positive for backward motion)
        self._set_wheel_pwm(-pwm_value, pwm_value)

    def turn(self, angle: float = 0.5):
        """
        Turn the robot.

        Args:
            angle: Turning angle from -1.0 to 1.0 (default: 0.5)
                  Positive for left, negative for right
        """
        if not -1.0 <= angle <= 1.0:
            raise ValueError("Angle must be between -1.0 and 1.0")

        # Convert angle to PWM values (0-885)
        pwm_value = int(abs(angle) * 885)

        if angle > 0:
            # Turn left (both wheels move in the same direction)
            self._set_wheel_pwm(-pwm_value, -pwm_value)
        else:
            # Turn right (both wheels move in the same direction)
            self._set_wheel_pwm(pwm_value, pwm_value)

    def _set_wheel_pwm(self, left_pwm: int, right_pwm: int):
        """
        Set PWM values for the wheel servos.

        Args:
            left_pwm: PWM value for the left wheel (-885 to 885)
            right_pwm: PWM value for the right wheel (-885 to 885)
        """
        # Ensure PWM values are within range
        left_pwm = max(min(left_pwm, 885), -885)
        right_pwm = max(min(right_pwm, 885), -885)

        # Set PWM values for wheel servos
        self.dynamixel.set_pwm_value(self.wheel_servo_ids[0], left_pwm)
        self.dynamixel.set_pwm_value(self.wheel_servo_ids[1], right_pwm)

    def stop(self):
        """Stop all movement."""
        # Stop wheels
        self._set_wheel_pwm(0, 0)

    def set_arm_position(self, positions: List[int]):
        """
        Set the arm joint positions.

        Args:
            positions: List of joint positions in range [0, 4096]
                      2048 is the center position
        """
        self.arm.set_goal_pos(positions)

    def get_arm_position(self) -> List[int]:
        """
        Get the current arm joint positions.

        Returns:
            List of joint positions in range [0, 4096]
        """
        return self.arm.read_position()

    def close(self):
        """Close the connection to the robot."""
        # Stop all movement
        self.stop()

        # Disable torque on all servos
        for motor_id in self.arm_servo_ids + self.wheel_servo_ids:
            self.dynamixel._disable_torque(motor_id)

        # Disconnect from Dynamixel
        self.dynamixel.disconnect()

        print("Maurice controller closed")
