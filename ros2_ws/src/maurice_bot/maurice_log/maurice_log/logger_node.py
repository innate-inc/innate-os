#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import BatteryState
from diagnostic_msgs.msg import DiagnosticArray
from std_msgs.msg import String

class LoggerNode(Node):
    def __init__(self):
        super().__init__('logger_node')
        self.get_logger().info('Logger node started')

        # Subscribers
        self.battery_sub = self.create_subscription(
            BatteryState,
            '/battery_state',
            self.battery_callback,
            10)
        self.diagnostics_sub = self.create_subscription(
            DiagnosticArray,
            '/diagnostics',
            self.diagnostics_callback,
            10)
        self.directive_sub = self.create_subscription(
            String,
            '/brain/set_directive',
            self.directive_callback,
            10)
        self.chat_out_sub = self.create_subscription(
            String,
            '/brain/chat_out',
            self.chat_out_callback,
            10)

    def battery_callback(self, msg):
        self.get_logger().info(f'Received battery_state: {msg}')

    def diagnostics_callback(self, msg):
        self.get_logger().info(f'Received diagnostics: {msg}')

    def directive_callback(self, msg):
        self.get_logger().info(f'Received directive: {msg.data}')

    def chat_out_callback(self, msg):
        self.get_logger().info(f'Received chat_out: {msg.data}')

def main(args=None):
    rclpy.init(args=args)
    node = LoggerNode()
    rclpy.spin(node)
    rclpy.shutdown()

if __name__ == '__main__':
    main()
