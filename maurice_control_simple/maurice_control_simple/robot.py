"""
Simplified Robot interface for Maurice robot.
"""

import numpy as np
import time
from dynamixel_sdk import (
    GroupSyncRead,
    GroupSyncWrite,
    DXL_LOBYTE,
    DXL_HIBYTE,
    DXL_LOWORD,
    DXL_HIWORD,
    COMM_SUCCESS,
)
from enum import Enum, auto
from typing import Union, List

from .dynamixel import Dynamixel, OperatingMode, ReadAttribute


class MotorControlType(Enum):
    PWM = auto()
    POSITION_CONTROL = auto()
    DISABLED = auto()
    UNKNOWN = auto()


class Robot:
    def __init__(self, dynamixel, servo_ids=None):
        """
        Initialize the robot with a Dynamixel interface.

        Args:
            dynamixel: Dynamixel interface
            servo_ids: List of servo IDs (default: [1, 2, 3, 4, 5])
        """
        self.servo_ids = servo_ids or [1, 2, 3, 4, 5]
        self.dynamixel = dynamixel

        # Initialize position reader
        self.position_reader = GroupSyncRead(
            self.dynamixel.portHandler,
            self.dynamixel.packetHandler,
            ReadAttribute.POSITION.value,
            4,
        )
        for id in self.servo_ids:
            self.position_reader.addParam(id)

        # Initialize velocity reader
        self.velocity_reader = GroupSyncRead(
            self.dynamixel.portHandler,
            self.dynamixel.packetHandler,
            ReadAttribute.VELOCITY.value,
            4,
        )
        for id in self.servo_ids:
            self.velocity_reader.addParam(id)

        # Initialize position writer
        self.pos_writer = GroupSyncWrite(
            self.dynamixel.portHandler,
            self.dynamixel.packetHandler,
            self.dynamixel.ADDR_GOAL_POSITION,
            4,
        )
        for id in self.servo_ids:
            self.pos_writer.addParam(
                id,
                [
                    DXL_LOBYTE(DXL_LOWORD(2048)),
                    DXL_HIBYTE(DXL_LOWORD(2048)),
                    DXL_LOBYTE(DXL_HIWORD(2048)),
                    DXL_HIBYTE(DXL_HIWORD(2048)),
                ],
            )

        # Initialize PWM writer
        self.pwm_writer = GroupSyncWrite(
            self.dynamixel.portHandler,
            self.dynamixel.packetHandler,
            self.dynamixel.ADDR_GOAL_PWM,
            2,
        )
        for id in self.servo_ids:
            self.pwm_writer.addParam(
                id,
                [
                    DXL_LOBYTE(DXL_LOWORD(0)),
                    DXL_HIBYTE(DXL_LOWORD(0)),
                ],
            )

        self.motor_control_state = MotorControlType.POSITION_CONTROL
        self._enable_torque()

    def read_position(self, tries=2):
        """
        Reads the joint positions of the robot.

        Args:
            tries: maximum number of tries to read the position

        Returns:
            list of joint positions in range [0, 4096]
        """
        result = self.position_reader.txRxPacket()
        if result != 0:
            if tries > 0:
                return self.read_position(tries=tries - 1)
            else:
                print("Failed to read position!")
                return [2048] * len(
                    self.servo_ids
                )  # Return center positions as fallback

        positions = []
        for id in self.servo_ids:
            position = self.position_reader.getData(id, ReadAttribute.POSITION.value, 4)
            if position > 2**31:
                position -= 2**32
            positions.append(position)
        return positions

    def read_velocity(self):
        """
        Reads the joint velocities of the robot.

        Returns:
            list of joint velocities
        """
        self.velocity_reader.txRxPacket()
        velocities = []
        for id in self.servo_ids:
            velocity = self.velocity_reader.getData(id, ReadAttribute.VELOCITY.value, 4)
            if velocity > 2**31:
                velocity -= 2**32
            velocities.append(velocity)
        return velocities

    def set_goal_pos(self, positions):
        """
        Set the goal positions for the servos.

        Args:
            positions: list or numpy array of target joint positions in range [0, 4096]
        """
        if not isinstance(positions, (list, np.ndarray)):
            raise TypeError("Positions must be a list or numpy array")

        if len(positions) != len(self.servo_ids):
            raise ValueError(
                f"Expected {len(self.servo_ids)} positions, got {len(positions)}"
            )

        if self.motor_control_state is not MotorControlType.POSITION_CONTROL:
            self._set_position_control()

        for i, motor_id in enumerate(self.servo_ids):
            data_write = [
                DXL_LOBYTE(DXL_LOWORD(int(positions[i]))),
                DXL_HIBYTE(DXL_LOWORD(int(positions[i]))),
                DXL_LOBYTE(DXL_HIWORD(int(positions[i]))),
                DXL_HIBYTE(DXL_HIWORD(int(positions[i]))),
            ]
            self.pos_writer.changeParam(motor_id, data_write)

        self.pos_writer.txPacket()

    def set_pwm(self, pwm_values):
        """
        Sets the PWM values for the servos.

        Args:
            pwm_values: list or numpy array of PWM values in range [0, 885]
        """
        if not isinstance(pwm_values, (list, np.ndarray)):
            raise TypeError("PWM values must be a list or numpy array")

        if len(pwm_values) != len(self.servo_ids):
            raise ValueError(
                f"Expected {len(self.servo_ids)} PWM values, got {len(pwm_values)}"
            )

        if self.motor_control_state is not MotorControlType.PWM:
            self._set_pwm_control()

        for i, motor_id in enumerate(self.servo_ids):
            data_write = [
                DXL_LOBYTE(DXL_LOWORD(int(pwm_values[i]))),
                DXL_HIBYTE(DXL_LOWORD(int(pwm_values[i]))),
            ]
            self.pwm_writer.changeParam(motor_id, data_write)

        self.pwm_writer.txPacket()

    def limit_pwm(self, limit: Union[int, list, np.ndarray]):
        """
        Limits the PWM values for the servos in position control.

        Args:
            limit: PWM limit (0 ~ 885) or list of limits
        """
        if isinstance(limit, int):
            limits = [limit] * len(self.servo_ids)
        else:
            limits = limit

        self._disable_torque()
        for motor_id, limit in zip(self.servo_ids, limits):
            self.dynamixel.set_pwm_limit(motor_id, limit)
        self._enable_torque()

    def _disable_torque(self):
        """Disable torque for all servos."""
        for motor_id in self.servo_ids:
            self.dynamixel._disable_torque(motor_id)

    def _enable_torque(self):
        """Enable torque for all servos."""
        for motor_id in self.servo_ids:
            self.dynamixel._enable_torque(motor_id)

    def _set_pwm_control(self):
        """Set all servos to PWM control mode."""
        self._disable_torque()
        for motor_id in self.servo_ids:
            self.dynamixel.set_operating_mode(motor_id, OperatingMode.PWM)
        self._enable_torque()
        self.motor_control_state = MotorControlType.PWM

    def _set_position_control(self):
        """Set all servos to position control mode."""
        self._disable_torque()
        for motor_id in self.servo_ids:
            self.dynamixel.set_operating_mode(motor_id, OperatingMode.POSITION)
        self._enable_torque()
        self.motor_control_state = MotorControlType.POSITION_CONTROL
