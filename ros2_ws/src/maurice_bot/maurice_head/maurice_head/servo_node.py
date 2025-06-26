#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from std_msgs.msg import Int32, String
from std_srvs.srv import SetBool  # Import service message type
from maurice_head.servo_controller import ServoController
import json

DEFAULT_ANGLE = 0  # Logical angle (will be offset to 220 on servo)
SERVO_OFFSET = 220  # Offset to apply to logical angles
MIN_ANGLE = -30
MAX_ANGLE = 70

class HeadServoNode(Node):
    def __init__(self):
        super().__init__('head_servo_node')
        self.declare_parameter('servo_id', 5)
        self.servo_id = self.get_parameter('servo_id').value
        self.servo_controller = ServoController(port="/dev/ttyACM0")
        
        # Track current position
        self.current_position = DEFAULT_ANGLE
        
        # Enable servo by default
        try:
            self.servo_controller.set_torque(self.servo_id, True)
            self.get_logger().info(f"Servo {self.servo_id} enabled by default")
        except Exception as e:
            self.get_logger().error(f"Failed to enable servo by default: {str(e)}")
        
        # Move to default position
        try:
            servo_position = DEFAULT_ANGLE + SERVO_OFFSET
            self.servo_controller.set_position(self.servo_id, servo_position)
            self.get_logger().info(f"Servo {self.servo_id} moved to default logical angle: {DEFAULT_ANGLE} (servo position: {servo_position})")
        except Exception as e:
            self.get_logger().error(f"Failed to move servo to default position: {str(e)}")
        
        # Subscription for position control
        self.subscription = self.create_subscription(
            Int32,
            'head/set_position',
            self.position_callback,
            10
        )
        
        # Publisher for current position (JSON format)
        self.position_publisher = self.create_publisher(
            String,
            'head/current_position',
            10
        )
        
        # Service for enabling/disabling servo
        self.srv = self.create_service(
            SetBool,
            'head/enable_servo',
            self.enable_servo_callback
        )
        
        # Service for getting current position
        self.position_srv = self.create_service(
            SetBool,  # We'll use SetBool for simplicity, but we could create a custom service
            'head/get_current_position',
            self.get_position_callback
        )
        
        # Publish initial position
        self.publish_position_status()
        self.get_logger().info(f"Published initial logical position: {self.current_position}")

    def publish_position_status(self):
        """Publish current position status as JSON with all position info."""
        position_data = {
            "current_position": self.current_position,
            "min_angle": MIN_ANGLE,
            "max_angle": MAX_ANGLE,
            "default_angle": DEFAULT_ANGLE
        }
        
        position_msg = String()
        position_msg.data = json.dumps(position_data)
        self.position_publisher.publish(position_msg)

    def position_callback(self, msg):
        logical_position = msg.data
        self.get_logger().info(f"Received position command: {logical_position}")
        
        # Validate position range
        if logical_position < MIN_ANGLE or logical_position > MAX_ANGLE:
            self.get_logger().error(f"Position {logical_position} out of range [{MIN_ANGLE}, {MAX_ANGLE}]")
            return
        
        try:
            # Apply offset for servo control
            servo_position = logical_position + SERVO_OFFSET
            self.servo_controller.set_position(self.servo_id, servo_position)
            
            # Update tracked position (use logical position) and publish it
            self.current_position = logical_position
            self.publish_position_status()
            self.get_logger().info(f"Updated current logical position to: {self.current_position} (servo position: {servo_position})")
        except Exception as e:
            self.get_logger().error(f"Failed to set position: {str(e)}")

    def enable_servo_callback(self, request, response):
        """Service callback to enable or disable the servo."""
        try:
            if request.data:
                # Enable servo logic here
                self.get_logger().info(f"Enabling servo {self.servo_id}")
                # Add your servo enable code here
                self.servo_controller.set_torque(self.servo_id, True)
                response.success = True
                response.message = f"Servo {self.servo_id} enabled"
            else:
                # Disable servo logic here
                self.get_logger().info(f"Disabling servo {self.servo_id}")
                # Add your servo disable code here
                self.servo_controller.set_torque(self.servo_id, False)
                response.success = True
                response.message = f"Servo {self.servo_id} disabled"
        except Exception as e:
            response.success = False
            response.message = f"Failed to set servo state: {str(e)}"
        return response

    def get_position_callback(self, request, response):
        """Service callback to get the current servo position."""
        try:
            response.success = True
            response.message = f"Current logical position: {self.current_position} (range: {MIN_ANGLE} to {MAX_ANGLE})"
            self.get_logger().info(f"Current logical position requested: {self.current_position}")
            
            # Also publish current position when requested
            self.publish_position_status()
        except Exception as e:
            response.success = False
            response.message = f"Failed to get current position: {str(e)}"
        return response

def main(args=None):
    rclpy.init(args=args)
    node = HeadServoNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()