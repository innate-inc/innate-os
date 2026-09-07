#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""ROS 2 node that collects robot vitals and forwards them to the cloud logger."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable

import psutil
import rclpy
from auth_client import AuthError, AuthProvider
from diagnostic_msgs.msg import DiagnosticArray
from dotenv import find_dotenv, load_dotenv
from mars_msgs.msg import ArmStatus
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from sensor_msgs.msg import BatteryState
from std_msgs.msg import String

from innate_logger import disks, host
from innate_logger.client import TelemetryClient

DEFAULT_TELEMETRY_URL = "https://logs.svc.innate.bot"
DEFAULT_AUTH_ISSUER_URL = "https://auth-v1.svc.innate.bot"


class LoggerNode(Node):
    """Subscribes to robot topics and logs telemetry at a throttled rate.

    Every cadence is a live parameter, compared against elapsed time on a fast
    tick rather than baked into a timer period — so lowering one in rqt takes
    effect on the next tick instead of at the next restart.
    """

    TICK: float = 1.0  # the floor on every interval below

    LOG_INTERVAL: float = 5.0
    # Battery, thermals and the memory breakdown. The voltage behind
    # /battery_state only refreshes on a 60s I2C health request, so every tick
    # would ship the same reading a dozen times.
    DETAIL_INTERVAL: float = 30.0
    DISK_INTERVAL: float = 3600.0
    # Sessions are often shorter than DISK_INTERVAL, so report once early too.
    DISK_BOOT_DELAY: float = 240.0

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
            "auth_issuer_url",
            os.getenv("INNATE_AUTH_URL", DEFAULT_AUTH_ISSUER_URL),
        )
        self.declare_parameter("log_interval", self.LOG_INTERVAL)
        self.declare_parameter("detail_interval", self.DETAIL_INTERVAL)
        self.declare_parameter("disk_interval", self.DISK_INTERVAL)
        self.declare_parameter("disk_boot_delay", self.DISK_BOOT_DELAY)

        telemetry_url: str = str(self.get_parameter("telemetry_url").get_parameter_value().string_value)
        # Never declare credentials as ROS params: rosbridge can read them.
        service_key: str = os.getenv("INNATE_SERVICE_KEY", "")
        auth_issuer: str = str(self.get_parameter("auth_issuer_url").value)

        if not service_key:
            message = "INNATE_SERVICE_KEY is required; set it in the environment or .env and restart the node"
            self.get_logger().fatal(message)
            raise RuntimeError(message)

        # ── Auth + telemetry client ─────────────────────────────────
        auth = AuthProvider(issuer_url=auth_issuer, service_key=service_key)
        # Cold boots can fail auth for several minutes (DNS not ready, or no
        # RTC so TLS sees the server cert as "not yet valid").  Prime the JWT
        # up-front with AuthProvider's retry helper so the periodic telemetry
        # timer doesn't silently paper over those errors.
        auth.wait_for_token(on_retry=self._log_auth_retry)
        self._client = TelemetryClient(url=telemetry_url, auth=auth)

        # Git commit at startup
        self._git_commit: str = self._get_git_commit()

        # Initialise CPU baseline (first call always returns 0.0%)
        psutil.cpu_percent()

        # ── Subscriptions ───────────────────────────────────────────
        self._latest_battery: BatteryState | None = None
        self._latest_diagnostics: DiagnosticArray | None = None
        self._robot_id: str | None = None
        self._mac_address: str | None = None
        self._hardware_revision: str | None = None
        self._version: str | None = None
        self._latest_arm: ArmStatus | None = None
        self._pending_disks: dict[str, object] | None = None
        self._last_error: dict[str, float] = {}

        # None means "not yet", which is what the boot delay is measured against.
        self._started: float = time.monotonic()
        self._last_vitals: float | None = None
        self._last_detail: float | None = None
        self._last_disks: float | None = None

        self.create_subscription(ArmStatus, "/mars/arm/status", self._on_arm_status, 1)
        self.create_subscription(BatteryState, "/battery_state", self._on_battery, 1)
        self.create_subscription(DiagnosticArray, "/diagnostics", self._on_diagnostics, 1)
        self.create_subscription(String, "/brain/set_directive", self._on_directive, 10)
        self.create_subscription(String, "/robot/info", self._on_robot_info, 1)

        self.create_timer(self.TICK, lambda: self._guarded(self._on_tick, "vitals"))

        # A disk sweep can block for seconds; its own group keeps it off the
        # thread the vitals tick runs on, and serialises the sweeps against
        # each other.
        self.create_timer(
            self.TICK,
            lambda: self._guarded(self._on_disk_tick, "disk sweep"),
            callback_group=MutuallyExclusiveCallbackGroup(),
        )

    # ── Helpers ─────────────────────────────────────────────────────

    def _seconds(self, parameter: str) -> float:
        """A cadence parameter, read live so an rqt edit lands on the next tick."""
        return max(self.TICK, float(self.get_parameter(parameter).value))

    def _elapsed(self, since: float | None) -> float:
        return time.monotonic() - (self._started if since is None else since)

    def _guarded(self, work: Callable[[], None], what: str) -> None:
        """Run a timer callback so a bad read cannot take the node with it.

        A thermal zone that refused to be read killed this node outright once;
        a process whose whole job is reporting health must degrade, not die.
        """
        try:
            work()
        except Exception as e:  # noqa: BLE001 — a logger that dies reports nothing at all
            # Throttled by hand: rclpy keys throttle_duration_sec on the call
            # site, which both timers share — one failing stream muted the other.
            now = time.monotonic()
            if now - self._last_error.get(what, float("-inf")) >= 60.0:
                self._last_error[what] = now
                self.get_logger().error(f"{what} failed: {e}")

    def _log_auth_retry(self, attempt: int, error: AuthError, next_delay: float) -> None:
        self.get_logger().warning(f"Auth not ready yet (attempt {attempt}): {error} — retrying in {next_delay:.0f}s")

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

    def _on_arm_status(self, msg: ArmStatus) -> None:
        self._latest_arm = msg

    def _on_diagnostics(self, msg: DiagnosticArray) -> None:
        self._latest_diagnostics = msg

    def _on_robot_info(self, msg: String) -> None:
        """Cache robot identity from mars_control's /robot/info.

        Either field can be null or absent — robot_id until the robot is
        provisioned, device_id when there is no Bluetooth adapter — so keep any
        previously seen value rather than clearing it.
        """
        try:
            info = json.loads(msg.data)
        except json.JSONDecodeError:
            info = None
        # Subscription callbacks have no _guarded boundary: a bare `null` here
        # would raise out of the executor and take the node down.
        if not isinstance(info, dict):
            self.get_logger().warning("Malformed JSON on /robot/info", throttle_duration_sec=60.0)
            return

        robot_id = info.get("robot_id")
        if robot_id:
            self._robot_id = str(robot_id)

        device_id = info.get("device_id")
        if device_id:
            self._mac_address = str(device_id)

        # robot_name and hostname are on this topic too, and stay off telemetry:
        # the name is user-chosen and the hostname is derived from it.
        if hardware_revision := info.get("hardware_revision"):
            self._hardware_revision = str(hardware_revision)

        if version := info.get("version"):
            self._version = str(version)

    def _on_directive(self, msg: String) -> None:
        self.get_logger().info(f"Received directive: {msg.data}")
        self._client.log_directive(msg.data)

    def _on_disk_tick(self) -> None:
        """Sweep once the boot delay has passed, then once per disk_interval."""
        wait = self._seconds("disk_boot_delay" if self._last_disks is None else "disk_interval")
        if self._elapsed(self._last_disks) < wait:
            return
        self._last_disks = time.monotonic()
        self._collect_disk_health()

    def _collect_disk_health(self) -> None:
        """Read disk health and queue it for the next vitals tick.

        Queued rather than posted directly so the report rides a complete vitals
        row, and survives a send failure to retry on the following tick.
        """
        report = disks.collect()
        if not report["smart"] and disks.devices():
            self.get_logger().warning(
                "No SMART data readable — install smartmontools and run `innate update reinstall`"
            )
        self._pending_disks = report
        self.get_logger().info(f"disks: {disks.summarize(report)}")

    def _on_tick(self) -> None:
        if self._elapsed(self._last_vitals) < self._seconds("log_interval"):
            return
        self._last_vitals = time.monotonic()
        self._log_vitals()

    def _log_vitals(self) -> None:
        """Collect cached vitals and send to the cloud logger."""
        cpu_usage = psutil.cpu_percent(interval=None)

        vitals: dict[str, object] = {
            "commit": self._git_commit,
            "cpu_usage": cpu_usage,
            **host.resources(),
        }

        for key, value in (
            ("robot_id", self._robot_id),
            ("mac_address", self._mac_address),
            ("hardware_revision", self._hardware_revision),
            ("version", self._version),
        ):
            if value is not None:
                vitals[key] = value

        sent_disks = self._pending_disks
        if sent_disks is not None:
            vitals["disks"] = sent_disks

        summary = f"cpu {cpu_usage:.0f}% | mem {vitals['memory_percent']}%"

        if self._latest_battery is not None:
            bat = self._latest_battery
            # In the summary on every tick, not just detail ticks: the 30s
            # console throttle and the 30s detail cadence run out of phase, so
            # a battery segment gated on detail never reaches the console.
            summary += f" | battery {bat.voltage:.2f}V ({bat.percentage:.0%})"

        if self._elapsed(self._last_detail) >= self._seconds("detail_interval"):
            self._last_detail = time.monotonic()
            vitals["temperatures"] = host.temperatures()
            vitals.update(host.memory_detail())
            if self._latest_battery is not None:
                # No power_supply_status: mars_bringup hardcodes it to
                # DISCHARGING, so it is the constant 2 on every robot forever.
                vitals["battery_voltage"] = self._latest_battery.voltage
                vitals["battery_percentage"] = self._latest_battery.percentage

        if self._latest_arm is not None:
            vitals["arm_ok"] = self._latest_arm.is_ok
            vitals["arm_torque_enabled"] = self._latest_arm.is_torque_enabled
            if not self._latest_arm.is_ok:
                vitals["arm_error"] = self._latest_arm.error
                summary += f" | arm {self._latest_arm.error}"

        if self._latest_diagnostics is not None:
            diag = self._latest_diagnostics
            if diag.status:
                entry = diag.status[0]
                level = entry.level[0] if isinstance(entry.level, bytes) else entry.level
                vitals["diagnostics_status"] = level
                vitals["diagnostics_message"] = entry.message
                vitals["diagnostics_hardware_id"] = entry.hardware_id
                summary += f" | diag [{level}] {entry.name}: {entry.message}"

        # One concise health line; full vitals still stream to the cloud every tick.
        self.get_logger().info(f"vitals: {summary}", throttle_duration_sec=30.0)
        # The exact body, for seeing what the cloud actually receives.
        self.get_logger().debug(f"POST /log/vitals {json.dumps(vitals, default=str)}")

        if not self._client.log_vitals(vitals):
            self.get_logger().warning(
                f"Telemetry unreachable — is {self._client.base_url} up?",
                throttle_duration_sec=20.0,
            )
            return

        # The POST blocks for seconds while a sweep runs on the other executor
        # thread; clearing unconditionally would drop a report that arrived
        # mid-flight and was never sent.
        if self._pending_disks is sent_disks:
            self._pending_disks = None


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = LoggerNode()
    rclpy.spin(node, executor=MultiThreadedExecutor(num_threads=2))
    rclpy.shutdown()


if __name__ == "__main__":
    main()
