"""
Simplified Dynamixel interface for Maurice robot.
"""

from dynamixel_sdk import (  # Uses Dynamixel SDK library
    PortHandler,
    PacketHandler,
)
import enum
import time


class ReadAttribute(enum.Enum):
    TEMPERATURE = 146
    VOLTAGE = 145
    VELOCITY = 128
    POSITION = 132
    CURRENT = 126
    PWM = 124
    HARDWARE_ERROR_STATUS = 70
    HOMING_OFFSET = 20
    BAUDRATE = 8


class OperatingMode(enum.Enum):
    VELOCITY = 1
    POSITION = 3
    CURRENT_CONTROLLED_POSITION = 5
    PWM = 16
    UNKNOWN = -1


class Dynamixel:
    ADDR_TORQUE_ENABLE = 64
    ADDR_GOAL_POSITION = 116
    ADDR_VELOCITY_LIMIT = 44
    ADDR_GOAL_PWM = 100
    ADDR_PWM_LIMIT = 36  # 2 bytes, PWM Limit (0 to 885)
    OPERATING_MODE_ADDR = 11
    POSITION_I = 82
    POSITION_P = 84
    ADDR_ID = 7
    ADDR_MAX_POSITION_LIMIT = 48  # 4 bytes, Max Position Limit
    ADDR_MIN_POSITION_LIMIT = 52  # 4 bytes, Min Position Limit
    ADDR_CURRENT_LIMIT = 38  # 2 bytes, Current Limit

    def __init__(
        self, baudrate=1000000, device_name="/dev/ttyACM0", protocol_version=2.0
    ):
        self.baudrate = baudrate
        self.device_name = device_name
        self.protocol_version = protocol_version

        # Initialize PortHandler and PacketHandler
        self.portHandler = PortHandler(self.device_name)
        self.packetHandler = PacketHandler(self.protocol_version)

        # Connect to the device
        self.connect()

    def connect(self):
        """Connect to the Dynamixel device."""
        if not self.portHandler.openPort():
            raise Exception(f"Failed to open port {self.device_name}")

        if not self.portHandler.setBaudRate(self.baudrate):
            raise Exception(f"Failed to set baud rate to {self.baudrate}")

        print(f"Connected to Dynamixel on {self.device_name} at {self.baudrate} baud")

    def disconnect(self):
        """Disconnect from the Dynamixel device."""
        self.portHandler.closePort()
        print("Disconnected from Dynamixel")

    def _enable_torque(self, motor_id):
        """Enable torque for a motor."""
        self.packetHandler.write1ByteTxRx(
            self.portHandler, motor_id, self.ADDR_TORQUE_ENABLE, 1
        )

    def _disable_torque(self, motor_id):
        """Disable torque for a motor."""
        self.packetHandler.write1ByteTxRx(
            self.portHandler, motor_id, self.ADDR_TORQUE_ENABLE, 0
        )

    def set_operating_mode(self, motor_id, mode):
        """Set the operating mode for a motor."""
        self.packetHandler.write1ByteTxRx(
            self.portHandler, motor_id, self.OPERATING_MODE_ADDR, mode.value
        )

    def set_pwm_limit(self, motor_id, limit):
        """Set the PWM limit for a motor."""
        self.packetHandler.write2ByteTxRx(
            self.portHandler, motor_id, self.ADDR_PWM_LIMIT, limit
        )

    def set_pwm_value(self, motor_id, value):
        """Set the PWM value for a motor."""
        self.packetHandler.write2ByteTxRx(
            self.portHandler, motor_id, self.ADDR_GOAL_PWM, value
        )

    def read_hardware_error_status(self, motor_id):
        """Read the hardware error status of a motor."""
        result, error, error_status = self.packetHandler.read1ByteTxRx(
            self.portHandler, motor_id, ReadAttribute.HARDWARE_ERROR_STATUS.value
        )
        return error_status

    def read_attribute(self, motor_id, attribute):
        """Read an attribute from a motor."""
        if attribute == ReadAttribute.POSITION:
            result, error, value = self.packetHandler.read4ByteTxRx(
                self.portHandler, motor_id, attribute.value
            )
        elif attribute in [
            ReadAttribute.VELOCITY,
            ReadAttribute.PWM,
            ReadAttribute.CURRENT,
        ]:
            result, error, value = self.packetHandler.read2ByteTxRx(
                self.portHandler, motor_id, attribute.value
            )
        else:
            result, error, value = self.packetHandler.read1ByteTxRx(
                self.portHandler, motor_id, attribute.value
            )
        return value

    def set_position(self, motor_id, position):
        """Set the goal position for a motor."""
        self.packetHandler.write4ByteTxRx(
            self.portHandler, motor_id, self.ADDR_GOAL_POSITION, position
        )
