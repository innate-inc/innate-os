#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from sensor_msgs.msg import BatteryState, LaserScan

class LoggerNode(Node):
    def __init__(self):
        super().__init__('logger_node')
        self.get_logger().info('Logger node started')

        # Subscribers
        self.cmd_vel_sub = self.create_subscription(
            Twist,
            '/cmd_vel',
            self.cmd_vel_callback,
            10)
        self.odom_sub = self.create_subscription(
            Odometry,
            '/odom',
            self.odom_callback,
            10)
        self.battery_sub = self.create_subscription(
            BatteryState,
            '/battery_state',
            self.battery_callback,
            10)
        self.scan_sub = self.create_subscription(
            LaserScan,
            '/scan',
            self.scan_callback,
            10)

    def cmd_vel_callback(self, msg):
        self.get_logger().info(f'Received cmd_vel: {msg}')

    def odom_callback(self, msg):
        self.get_logger().info(f'Received odom: {msg}')

    def battery_callback(self, msg):
        self.get_logger().info(f'Received battery_state: {msg}')

    def scan_callback(self, msg):
        self.get_logger().info(f'Received scan: {msg}')

def main(args=None):
    rclpy.init(args=args)
    node = LoggerNode()
    rclpy.spin(node)
    rclpy.shutdown()

if __name__ == '__main__':
    main()
