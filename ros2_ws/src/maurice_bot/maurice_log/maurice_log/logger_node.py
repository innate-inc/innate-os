#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import BatteryState
from diagnostic_msgs.msg import DiagnosticArray
from std_msgs.msg import String

class LoggerNode(Node):
    LOG_INTERVAL = 5.0  # 0.2Hz = every 5 seconds

    def __init__(self):
        super().__init__('logger_node')
        self.get_logger().info('Logger node started')

        # Latest message cache (updated on every message, logged on timer)
        self._latest_battery = None
        self._latest_diagnostics = None

        # Subscribers (queue depth 1 since we only care about latest)
        self.battery_sub = self.create_subscription(
            BatteryState,
            '/battery_state',
            self._on_battery,
            1)
        self.diagnostics_sub = self.create_subscription(
            DiagnosticArray,
            '/diagnostics',
            self._on_diagnostics,
            1)
        # Directive and chat_out are events - log immediately, don't throttle
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

        # Timer for throttled logging at 0.2Hz
        self.log_timer = self.create_timer(self.LOG_INTERVAL, self._log_vitals)

    def _on_battery(self, msg):
        self._latest_battery = msg

    def _on_diagnostics(self, msg):
        self._latest_diagnostics = msg

    def _log_vitals(self):
        """Log cached vitals at throttled rate."""
        if self._latest_battery is not None:
            self.get_logger().info(f'battery_state: {self._latest_battery}')
        if self._latest_diagnostics is not None:
            self.get_logger().info(f'diagnostics: {self._latest_diagnostics}')

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
