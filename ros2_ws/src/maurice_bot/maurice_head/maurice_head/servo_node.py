#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from std_msgs.msg import Int32, String
from std_srvs.srv import SetBool  # Import service message type


# Import Dynamixel classes from maurice_arm package
from maurice_arm.dynamixel import Dynamixel, OperatingMode
import json
import math
import time

DEFAULT_ANGLE = 0  # Logical angle
MIN_ANGLE = -40
MAX_ANGLE = 70


class HeadServoNode(Node):
    def __init__(self):
        super().__init__("head_servo_node")

        # Declare parameters
        self.declare_parameter("servo_id", 7)
        self.declare_parameter("device_name", "/dev/ttyACM1")
        self.declare_parameter("baud_rate", 1000000)
        self.declare_parameter("pwm_limit", 885)
        self.declare_parameter("current_limit", 500)
        self.declare_parameter(
            "position_offset", -445
        )  # Encoder offset for calibration
        self.declare_parameter(
            "control_frequency", 50
        )  # Hz - how often to publish position

        # Get parameters
        self.servo_id = self.get_parameter("servo_id").value
        device_name = self.get_parameter("device_name").value
        baud_rate = self.get_parameter("baud_rate").value
        pwm_limit = self.get_parameter("pwm_limit").value
        current_limit = self.get_parameter("current_limit").value
        self.position_offset = self.get_parameter("position_offset").value
        control_frequency = self.get_parameter("control_frequency").value

        # Initialize Dynamixel interface
        self.dynamixel = Dynamixel.Config(
            baudrate=baud_rate, device_name=device_name
        ).instantiate()

        self.get_logger().info(f"Connected to Dynamixel at {device_name}")

        self.get_logger().info(f"Using servo ID: {self.servo_id}")
        self.get_logger().info(
            f"Position offset: {self.position_offset} encoder counts"
        )

        # Configure servo
        self._configure_servo(pwm_limit, current_limit)

        # Track current position (in logical angles)
        self.current_position = DEFAULT_ANGLE

        # Move to default position
        self._move_to_logical_angle(DEFAULT_ANGLE)

        # Subscription for position control
        self.subscription = self.create_subscription(
            Int32, "head/set_position", self.position_callback, 10
        )

        # Publisher for current position (JSON format)
        self.position_publisher = self.create_publisher(
            String, "head/current_position", 10
        )

        # Service for enabling/disabling servo
        self.srv = self.create_service(
            SetBool, "head/enable_servo", self.enable_servo_callback
        )

        # Create timer for frequent position publishing
        self.timer = self.create_timer(1.0 / control_frequency, self.timer_callback)

        # Store the latest command for processing in timer callback
        self.latest_command = None

        # Publish initial position
        self.publish_position_status()
        self.get_logger().info(
            f"Published initial logical position: {self.current_position}"
        )
        self.get_logger().info(f"Control frequency: {control_frequency} Hz")

    def timer_callback(self):
        """Publish current servo position at control frequency and process any pending commands."""
        try:
            # Always publish current position
            self.publish_position_status()

            # If a new command was received, execute it
            if self.latest_command is not None:
                self._move_to_logical_angle(self.latest_command)
                self.latest_command = None

        except Exception as e:
            self.get_logger().error(f"Error in timer callback: {str(e)}")

    def _configure_servo(self, pwm_limit, current_limit):
        """Configure the servo with proper limits and operating mode"""
        try:
            # Disable torque for configuration
            self.get_logger().info(f"Disabling torque for servo {self.servo_id}")
            self.dynamixel._disable_torque(self.servo_id)
            time.sleep(0.5)

            # Convert angle limits to encoder values relative to actual servo center
            # Note: Since we reverse direction, MIN_ANGLE maps to max servo position and vice versa
            # Using: encoder_value = int((angle_rad / (2*pi)) * 4096 + 2048 + offset)
            min_servo_rad = math.radians(-MAX_ANGLE)  # Logical MAX becomes servo MIN
            max_servo_rad = math.radians(-MIN_ANGLE)  # Logical MIN becomes servo MAX
            min_encoder = int(
                (min_servo_rad / (2 * math.pi)) * 4096 + 2048 + self.position_offset
            )
            max_encoder = int(
                (max_servo_rad / (2 * math.pi)) * 4096 + 2048 + self.position_offset
            )

            self.get_logger().info(
                f"Setting position limits: {min_encoder} (min) to {max_encoder} (max)"
            )
            self.dynamixel.set_min_position_limit(self.servo_id, min_encoder)
            self.dynamixel.set_max_position_limit(self.servo_id, max_encoder)
            time.sleep(0.5)

            # Set PWM limit
            self.get_logger().info(f"Setting PWM limit: {pwm_limit}")
            self.dynamixel.set_pwm_limit(self.servo_id, pwm_limit)
            time.sleep(0.5)

            # Set current limit
            self.get_logger().info(f"Setting current limit: {current_limit}")
            self.dynamixel.set_current_limit(self.servo_id, current_limit)
            time.sleep(0.5)

            # Set operating mode to position control
            self.get_logger().info("Setting operating mode to POSITION")
            self.dynamixel.set_operating_mode(self.servo_id, OperatingMode.POSITION)
            time.sleep(0.5)

            # Enable torque
            self.get_logger().info(f"Enabling torque for servo {self.servo_id}")
            self.dynamixel._enable_torque(self.servo_id)
            time.sleep(0.5)

        except Exception as e:
            self.get_logger().error(f"Failed to configure servo: {str(e)}")
            raise

    def _logical_angle_to_encoder(self, logical_angle):
        """Convert logical angle to encoder value with offset and direction reversal"""
        # Reverse the angle direction (positive becomes negative, negative becomes positive)
        reversed_angle = -logical_angle
        # Convert logical angle to radians
        angle_rad = math.radians(reversed_angle)
        # Convert to encoder value and apply offset
        encoder_value = int(
            (angle_rad / (2 * math.pi)) * 4096 + 2048 + self.position_offset
        )
        return encoder_value

    def _encoder_to_logical_angle(self, encoder_value):
        """Convert encoder value to logical angle with offset and direction reversal"""
        # Convert encoder value to radians, removing offset
        angle_rad = (encoder_value - 2048 - self.position_offset) * (2 * math.pi) / 4096
        # Convert to degrees
        servo_angle = math.degrees(angle_rad)
        # Reverse the direction to get logical angle (since we reversed it when sending)
        logical_angle = -servo_angle
        return logical_angle

    def _move_to_logical_angle(self, logical_angle):
        """Move servo to logical angle"""
        try:
            encoder_value = self._logical_angle_to_encoder(logical_angle)
            self.dynamixel.set_goal_position(self.servo_id, encoder_value)
            self.current_position = logical_angle
            self.get_logger().info(
                f"Moved to logical angle: {logical_angle} (encoder: {encoder_value})"
            )
        except Exception as e:
            self.get_logger().error(
                f"Failed to move to logical angle {logical_angle}: {str(e)}"
            )

    def _read_current_position(self):
        """Read current position from servo and return as logical angle"""
        try:
            encoder_value = self.dynamixel.read_position(self.servo_id)
            logical_angle = self._encoder_to_logical_angle(encoder_value)
            return logical_angle
        except Exception as e:
            self.get_logger().error(f"Failed to read position: {str(e)}")
            return self.current_position  # Return cached position on error

    def publish_position_status(self):
        """Publish current position status as JSON with all position info."""
        # Read actual position from servo
        actual_position = self._read_current_position()

        position_data = {
            "current_position": actual_position,
            "min_angle": MIN_ANGLE,
            "max_angle": MAX_ANGLE,
            "default_angle": DEFAULT_ANGLE,
        }

        position_msg = String()
        position_msg.data = json.dumps(position_data)
        self.position_publisher.publish(position_msg)

    def position_callback(self, msg):
        logical_position = msg.data
        self.get_logger().info(f"Received position command: {logical_position}")

        # Validate position range
        if logical_position < MIN_ANGLE or logical_position > MAX_ANGLE:
            self.get_logger().error(
                f"Position {logical_position} out of range [{MIN_ANGLE}, {MAX_ANGLE}]"
            )
            return

        try:
            # Store the command for processing in the timer callback
            self.latest_command = logical_position

        except Exception as e:
            self.get_logger().error(f"Failed to store position command: {str(e)}")

    def enable_servo_callback(self, request, response):
        """Service callback to enable or disable the servo."""
        try:
            if request.data:
                # Enable servo
                self.get_logger().info(f"Enabling servo {self.servo_id}")
                self.dynamixel._enable_torque(self.servo_id)
                response.success = True
                response.message = f"Servo {self.servo_id} enabled"
            else:
                # Disable servo
                self.get_logger().info(f"Disabling servo {self.servo_id}")
                self.dynamixel._disable_torque(self.servo_id)
                response.success = True
                response.message = f"Servo {self.servo_id} disabled"
        except Exception as e:
            response.success = False
            response.message = f"Failed to set servo state: {str(e)}"
        return response


def main(args=None):
    rclpy.init(args=args)
    node = HeadServoNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
