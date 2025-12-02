#!/usr/bin/env python3

import json
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import BatteryState
from diagnostic_msgs.msg import DiagnosticArray
from std_msgs.msg import String

from maurice_log.bigquery_logger import RobotTelemetryLogger


class LoggerNode(Node):
    LOG_INTERVAL = 5.0  # 0.2Hz = every 5 seconds

    def __init__(self):
        super().__init__('logger_node')
        self.get_logger().info('Logger node started')

        # Declare and get telemetry URL parameter
        self.declare_parameter('telemetry_url', 'https://logs.innate.bot')
        telemetry_url = self.get_parameter('telemetry_url').get_parameter_value().string_value

        # Telemetry logger (initialized without robot_id, set when /robot/info received)
        self.bq_logger = RobotTelemetryLogger(url=telemetry_url, robot_id=None)

        # Subscribe to robot info to get robot_id
        self.robot_info_sub = self.create_subscription(
            String,
            '/robot/info',
            self._on_robot_info,
            10)

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
        # Directive events - log immediately, don't throttle
        self.directive_sub = self.create_subscription(
            String,
            '/brain/set_directive',
            self.directive_callback,
            10)

        # Timer for throttled logging at 0.2Hz
        self.log_timer = self.create_timer(self.LOG_INTERVAL, self._log_vitals)

    def _on_robot_info(self, msg):
        """Handle robot info updates from /robot/info topic."""
        try:
            data = json.loads(msg.data)
            robot_id = data.get("robot_id")
            if robot_id and robot_id != self.bq_logger.robot_id:
                self.bq_logger.robot_id = robot_id
                self.bq_logger.enabled = bool(self.bq_logger.base_url and robot_id)
                self.get_logger().info(f'Robot ID set: {robot_id}')
        except json.JSONDecodeError:
            pass

    def _on_battery(self, msg):
        self._latest_battery = msg

    def _on_diagnostics(self, msg):
        self._latest_diagnostics = msg

    def _log_vitals(self):
        """Log cached vitals at throttled rate to console and BigQuery."""
        if self._latest_battery is not None:
            bat = self._latest_battery
            self.get_logger().info(
                f'battery: {bat.voltage:.2f}V ({bat.percentage:.1%})'
            )
            self.bq_logger.log_battery(
                voltage=bat.voltage,
                percentage=bat.percentage,
                status=bat.power_supply_status,
                health=bat.power_supply_health,
            )

        if self._latest_diagnostics is not None:
            diag = self._latest_diagnostics
            # Log first status entry if available
            if diag.status:
                entry = diag.status[0]
                # entry.level is bytes, convert to int
                level = entry.level[0] if isinstance(entry.level, bytes) else entry.level
                self.get_logger().info(
                    f'diagnostics: [{level}] {entry.name}: {entry.message}'
                )
                self.bq_logger.log_diagnostics(
                    status=level,
                    message=entry.message,
                    hardware_id=entry.hardware_id,
                )

    def directive_callback(self, msg):
        self.get_logger().info(f'Received directive: {msg.data}')
        self.bq_logger.log_directive(msg.data)

def main(args=None):
    rclpy.init(args=args)
    node = LoggerNode()
    rclpy.spin(node)
    rclpy.shutdown()

if __name__ == '__main__':
    main()
