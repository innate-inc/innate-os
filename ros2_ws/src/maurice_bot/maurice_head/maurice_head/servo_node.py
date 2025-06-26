#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from std_msgs.msg import Int32
from std_srvs.srv import SetBool  # Import service message type
from maurice_head.servo_controller import ServoController

class HeadServoNode(Node):
    def __init__(self):
        super().__init__('head_servo_node')
        self.declare_parameter('servo_id', 5)
        self.servo_id = self.get_parameter('servo_id').value
        self.servo_controller = ServoController(port="/dev/ttyACM0")
        
        # Subscription for position control
        self.subscription = self.create_subscription(
            Int32,
            'head/set_position',
            self.position_callback,
            10
        )
        
        # Service for enabling/disabling servo
        self.srv = self.create_service(
            SetBool,
            'head/enable_servo',
            self.enable_servo_callback
        )

    def position_callback(self, msg):
        self.get_logger().info(f"Received position command: {msg.data}")
        self.servo_controller.set_position(self.servo_id, msg.data)

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

def main(args=None):
    rclpy.init(args=args)
    node = HeadServoNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()

if __name__ == '__main__':
    main()