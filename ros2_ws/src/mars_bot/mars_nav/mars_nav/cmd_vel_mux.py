#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""cmd_vel priority mux — one writer to the base at a time.

Forwards messages from the highest-priority source that has published within
its freshness window:

    /cmd_vel_teleop > /cmd_vel_skills > /cmd_vel_nav  ->  /cmd_vel

Event-driven (no re-timing). One zero twist is published when every source
goes stale so the base stops deterministically.  Mapping modes apply the
robot's Slow envelope at this final common path, so Fast/Mad teleop settings,
skills, Nav2, and recovery behaviors cannot bypass it.
"""

import time
from functools import partial

import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import SetBool

from mars_nav.navigation_policy import (
    DEFAULT_MANUAL_MAX_ANGULAR_RPS,
    DEFAULT_MANUAL_MAX_LINEAR_MPS,
    DEFAULT_NAV_MAX_ANGULAR_RPS,
    DEFAULT_NAV_MAX_LINEAR_MPS,
    MAPPING_SPEED_LIMIT_SERVICE,
    mapping_slow_limits,
    mapping_speed_factor,
    mapping_speed_limit_active,
)

# (name, topic, freshness window s) — highest priority first. Windows exceed
# each source's publish interval so an active source never flickers stale.
SOURCES = [
    ("teleop", "/cmd_vel_teleop", 0.5),
    ("skills", "/cmd_vel_skills", 0.5),
    ("nav", "/cmd_vel_nav", 0.5),
]


class CmdVelMux(Node):
    def __init__(self):
        super().__init__("cmd_vel_mux")
        self.declare_parameter("motion_control.max_speed", DEFAULT_MANUAL_MAX_LINEAR_MPS)
        self.declare_parameter("motion_control.max_angular_speed", DEFAULT_MANUAL_MAX_ANGULAR_RPS)
        self.declare_parameter("nav.max_speed", DEFAULT_NAV_MAX_LINEAR_MPS)
        self.declare_parameter("nav.max_angular_speed", DEFAULT_NAV_MAX_ANGULAR_RPS)
        self._mapping_max_linear, self._mapping_max_angular = mapping_slow_limits(
            float(self.get_parameter("motion_control.max_speed").value),
            float(self.get_parameter("motion_control.max_angular_speed").value),
            float(self.get_parameter("nav.max_speed").value),
            float(self.get_parameter("nav.max_angular_speed").value),
        )

        # Fail slow until mode_manager synchronously establishes an
        # authoritative state. The mode topic remains a compatibility fallback
        # only until the first acknowledged SetBool request.
        self._mapping_limit_enabled = True
        self._has_authoritative_limit_source = False
        self._pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self._last_rx = {name: 0.0 for name, _topic, _window in SOURCES}
        self._forwarding = False  # motion since the last all-stale zero-stop
        mode_qos = QoSProfile(
            depth=1,
            reliability=QoSReliabilityPolicy.RELIABLE,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._mode_sub = self.create_subscription(String, "/nav/current_mode", self._on_mode, mode_qos)
        self._mapping_limit_service = self.create_service(
            SetBool,
            MAPPING_SPEED_LIMIT_SERVICE,
            self._set_mapping_speed_limit,
        )
        for priority, (name, topic, _window) in enumerate(SOURCES):
            self.create_subscription(Twist, topic, partial(self._on_twist, priority, name), 10)
        self.create_timer(0.1, self._stop_when_all_stale)
        self.get_logger().info("cmd_vel mux up: " + " > ".join(f"{name} ({topic})" for name, topic, _w in SOURCES))

    def _on_twist(self, priority, name, msg):
        now = time.monotonic()
        self._last_rx[name] = now
        override = self._fresh_source_above(priority, now)
        if override:
            self.get_logger().info(f"'{override}' overriding '{name}' on /cmd_vel", throttle_duration_sec=2.0)
            return
        self._pub.publish(self._apply_mapping_speed_limit(msg))
        self._forwarding = True

    def _on_mode(self, msg):
        if self._has_authoritative_limit_source:
            self.get_logger().debug(f"Ignoring fallback mapping envelope update from /nav/current_mode ({msg.data!r})")
            return

        enabled = mapping_speed_limit_active(msg.data)
        was_enabled = self._mapping_limit_enabled
        self._mapping_limit_enabled = enabled
        if enabled and not was_enabled:
            # A legacy mode manager has entered switching/mapping. Replace any
            # previously forwarded unrestricted command before accepting more.
            self._publish_zero_stop()

    def _set_mapping_speed_limit(self, request, response):
        """Synchronously establish the authoritative final-drive envelope."""

        self._has_authoritative_limit_source = True
        self._mapping_limit_enabled = bool(request.data)
        if self._mapping_limit_enabled:
            # publish() runs before this callback returns, so a successful
            # service acknowledgement can never precede the replacing zero.
            self._publish_zero_stop()
        response.success = True
        response.message = (
            "Mapping Slow envelope enabled" if self._mapping_limit_enabled else "Mapping Slow envelope disabled"
        )
        return response

    def _publish_zero_stop(self):
        self._forwarding = False
        self._pub.publish(Twist())

    def _apply_mapping_speed_limit(self, msg):
        if not self._mapping_limit_enabled:
            return msg

        factor = mapping_speed_factor(
            True,
            float(msg.linear.x),
            float(msg.angular.z),
            self._mapping_max_linear,
            self._mapping_max_angular,
        )
        # MARS is differential drive. While the envelope is active, explicitly
        # zero every unsupported Twist axis so a source cannot bypass the cap
        # through linear.y/z or angular.x/y.
        limited = Twist()
        if factor <= 0.0:
            return limited
        limited.linear.x = msg.linear.x * factor
        limited.angular.z = msg.angular.z * factor
        return limited

    def _fresh_source_above(self, priority, now):
        """Name of a higher-priority source still inside its freshness window."""
        for name, _topic, window in SOURCES[:priority]:
            if now - self._last_rx[name] < window:
                return name
        return None

    def _stop_when_all_stale(self):
        """One deterministic zero after the last active source goes quiet."""
        if not self._forwarding:
            return
        now = time.monotonic()
        if any(now - self._last_rx[name] < window for name, _topic, window in SOURCES):
            return
        self._forwarding = False
        self._pub.publish(Twist())
        self.get_logger().debug("all cmd_vel sources stale — published zero stop")


def main(args=None):
    rclpy.init(args=args)
    node = CmdVelMux()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
