#!/usr/bin/env python3
"""ROS 2 node that collects robot vitals and forwards them to the cloud logger."""

from __future__ import annotations

import json
import os
import subprocess
from typing import Optional

import psutil
import rclpy
from dotenv import find_dotenv, load_dotenv
from rclpy.node import Node
from sensor_msgs.msg import BatteryState
from diagnostic_msgs.msg import DiagnosticArray
from std_msgs.msg import String

from auth_client import AuthProvider

from innate_logger.client import TelemetryClient

DEFAULT_TELEMETRY_URL = "https://logs-v1.innate.bot"
DEFAULT_AUTH_ISSUER_URL = "https://auth-v1.innate.bot"


class LoggerNode(Node):
    """Subscribes to robot topics and logs telemetry at a throttled rate."""

    LOG_INTERVAL: float = 5.0  # seconds (0.2 Hz)

    def __init__(self) -> None:
        super().__init__("logger_node")
        self.get_logger().info("Logger node started")

        # Load .env (same pattern as innate_training_node)
        env_path = find_dotenv(usecwd=True)
        if env_path:
            load_dotenv(env_path)
            self.get_logger().info(f"Loaded .env from {env_path}")
        else:
            self.get_logger().info("No .env file found")

        # ── Parameters ──────────────────────────────────────────────
        self.declare_parameter(
            "telemetry_url",
            os.getenv("TELEMETRY_URL", DEFAULT_TELEMETRY_URL),
        )
        self.declare_parameter(
            "service_key",
            os.getenv("INNATE_SERVICE_KEY", ""),
        )
        self.declare_parameter(
            "auth_issuer_url",
            os.getenv("INNATE_AUTH_URL", DEFAULT_AUTH_ISSUER_URL),
        )

        telemetry_url: str = str(
            self.get_parameter("telemetry_url").get_parameter_value().string_value
        )
        service_key: str = str(self.get_parameter("service_key").value)
        auth_issuer: str = str(self.get_parameter("auth_issuer_url").value)

        if not service_key:
            self.get_logger().fatal("service_key is required")
            raise RuntimeError("service_key is required")

        # ── Auth + telemetry client ─────────────────────────────────
        auth = AuthProvider(issuer_url=auth_issuer, service_key=service_key)
        self._client = TelemetryClient(url=telemetry_url, auth=auth)

        # Git commit at startup
        self._git_commit: str = self._get_git_commit()

        # Initialise CPU baseline (first call always returns 0.0%)
        psutil.cpu_percent()

        # ── Subscriptions ───────────────────────────────────────────
        self._latest_battery: Optional[BatteryState] = None
        self._latest_diagnostics: Optional[DiagnosticArray] = None

        self.create_subscription(BatteryState, "/battery_state", self._on_battery, 1)
        self.create_subscription(
            DiagnosticArray, "/diagnostics", self._on_diagnostics, 1
        )
        self.create_subscription(
            String, "/brain/set_directive", self._on_directive, 10
        )

        # Timer for vitals logging
        self.create_timer(self.LOG_INTERVAL, self._log_vitals)

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _get_git_commit() -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else "unknown"
        except Exception:
            return "unknown"

    # ── Callbacks ───────────────────────────────────────────────────


    def _on_battery(self, msg: BatteryState) -> None:
        self._latest_battery = msg

    def _on_diagnostics(self, msg: DiagnosticArray) -> None:
        self._latest_diagnostics = msg

    def _on_directive(self, msg: String) -> None:
        self.get_logger().info(f"Received directive: {msg.data}")
        self._client.log_directive(msg.data)

    def _log_vitals(self) -> None:
        """Collect cached vitals and send to the cloud logger."""
        cpu_usage = psutil.cpu_percent(interval=None)

        vitals: dict[str, object] = {
            "commit": self._git_commit,
            "cpu_usage": cpu_usage,
        }

        if self._latest_battery is not None:
            bat = self._latest_battery
            vitals["battery_voltage"] = bat.voltage
            vitals["battery_percentage"] = bat.percentage
            vitals["battery_status"] = bat.power_supply_status
            vitals["battery_health"] = bat.power_supply_health
            self.get_logger().info(
                f"battery: {bat.voltage:.2f}V ({bat.percentage:.1%})"
            )

        if self._latest_diagnostics is not None:
            diag = self._latest_diagnostics
            if diag.status:
                entry = diag.status[0]
                level = (
                    entry.level[0]
                    if isinstance(entry.level, bytes)
                    else entry.level
                )
                vitals["diagnostics_status"] = level
                vitals["diagnostics_message"] = entry.message
                vitals["diagnostics_hardware_id"] = entry.hardware_id
                self.get_logger().info(
                    f"diagnostics: [{level}] {entry.name}: {entry.message}"
                )

        self.get_logger().info(f"cpu: {cpu_usage:.1f}%")

        if not self._client.log_vitals(vitals):
            self.get_logger().warning(
                f"Telemetry unreachable — is {self._client.base_url} up?",
                throttle_duration_sec=20.0,
            )


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = LoggerNode()
    rclpy.spin(node)
    rclpy.shutdown()


if __name__ == "__main__":
    main()
