#!/usr/bin/env python3
"""
Send a sequence of cmd_vel commands with timed durations.
Each step specifies (linear_x m/s, angular_z rad/s, duration_sec).
A zero-velocity pause is inserted between moves.

Usage:
  python3 cmd_vel_sequence.py
  ros2 run --prefix 'python3' ... (or just run directly)
"""

import math
import time

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from std_msgs.msg import Float32

# ── Velocity sequence ──────────────────────────────────────────────
# Each entry: (linear_x m/s, angular_z rad/s, duration s)
# A (0, 0, t) entry is a pause.
SEQUENCE = [
    (1.00, 0.0, 1),
    # 1. Forward 0.1 m/s for 0.2 s
    # (-1.00, 0.0, 0.2),
    # (-0.50, 0.0, 0.2),
    # (-1.00, 0.0, 1.7),
    # pause
    (-0.01,  0.0, 1.0),
    (0.00,  0.0, 0.2),
    (1.00, 0.0, 1),
    (0.00, 0.0, 1),
    (-1.00, 0.0, 3.8),
    (0.00, 0.0, 1),
    # (0.01, 0.0, 0.2),

    # # 2. Backward 0.1 m/s for 0.2 s
    # (-0.10, 0.0, 0.2),
    # # pause
    # (0.0,   0.0, 0.3),

    # # 3. Rotate left 0.3 rad/s for 0.4 s
    # (0.0, 0.30, 0.4),
    # # pause
    # (0.0, 0.0,  0.3),

    # # 4. Rotate right 0.3 rad/s for 0.4 s
    # (0.0, -0.30, 0.4),
    # # pause
    # (0.0,  0.0,  0.3),

    # # 5. Forward + slight left arc
    # (0.10, 0.15, 0.5),
    # # pause
    # (0.0,  0.0,  0.3),

    # # 6. Forward + slight right arc
    # (0.10, -0.15, 0.5),
    # final stop
    (0.0,  0.0,  0.3),
]

PUBLISH_HZ = 50  # how often we re-publish during each step
TURN_P = 0.011     # desired turn P gain sent to MCU (0 = leave unchanged)


class CmdVelSequence(Node):
    def __init__(self):
        super().__init__("cmd_vel_sequence")
        self.pub = self.create_publisher(Twist, "/cmd_vel_scaled", 10)
        self.turn_p_pub = self.create_publisher(Float32, "/set_turn_p", 10)
        self.odom_sub = self.create_subscription(Odometry, "/odom", self._odom_cb, 10)
        self._latest_odom: Odometry | None = None

    def send_turn_p(self, p: float):
        """Publish turn P gain so bringup forwards it to the MCU."""
        msg = Float32()
        msg.data = p
        self.turn_p_pub.publish(msg)
        self.get_logger().info(f"Published turn_p={p:.2f} to /set_turn_p")

    def _odom_cb(self, msg: Odometry):
        self._latest_odom = msg

    @staticmethod
    def _yaw_from_quat(q):
        siny_cosp = 2.0 * (q.w * q.z + q.x * q.y)
        cosy_cosp = 1.0 - 2.0 * (q.y * q.y + q.z * q.z)
        return math.atan2(siny_cosp, cosy_cosp)

    def _wait_for_odom(self, timeout: float = 5.0) -> Odometry:
        """Spin until we have a fresh odom message."""
        self._latest_odom = None
        t0 = time.monotonic()
        while self._latest_odom is None:
            rclpy.spin_once(self, timeout_sec=0.05)
            if time.monotonic() - t0 > timeout:
                self.get_logger().warn("Timed out waiting for /odom – continuing without it")
                return None
        return self._latest_odom

    @staticmethod
    def _integrate_sequence():
        x, y, yaw = 0.0, 0.0, 0.0
        for lx, az, dt in SEQUENCE:
            if abs(az) < 1e-9:
                x += lx * dt * math.cos(yaw)
                y += lx * dt * math.sin(yaw)
            else:
                x += (lx / az) * (math.sin(yaw + az * dt) - math.sin(yaw))
                y += (lx / az) * (-math.cos(yaw + az * dt) + math.cos(yaw))
            yaw += az * dt
        return x, y, yaw

    def run_sequence(self):
        # ── Send turn P gain if nonzero ──
        self.send_turn_p(TURN_P)
        time.sleep(0.1)  # give bringup time to process

        # ── Capture start odom ──
        self.get_logger().info("Waiting for initial /odom …")
        odom_start = self._wait_for_odom()
        if odom_start:
            p = odom_start.pose.pose.position
            yaw0 = self._yaw_from_quat(odom_start.pose.pose.orientation)
            self.get_logger().info(f"Start odom  x={p.x:.4f}  y={p.y:.4f}  yaw={math.degrees(yaw0):.2f}°")

        # ── Print per-step theoretical deltas ──
        self.get_logger().info("── Theoretical per-step displacement (cumulative) ──")
        cx, cy, cyaw = 0.0, 0.0, 0.0
        for i, (lx, az, dt) in enumerate(SEQUENCE):
            if abs(az) < 1e-9:
                cx += lx * dt * math.cos(cyaw)
                cy += lx * dt * math.sin(cyaw)
            else:
                cx += (lx / az) * (math.sin(cyaw + az * dt) - math.sin(cyaw))
                cy += (lx / az) * (-math.cos(cyaw + az * dt) + math.cos(cyaw))
            cyaw += az * dt
            self.get_logger().info(
                f"  after step {i+1}: dx={cx:+.4f}  dy={cy:+.4f}  dyaw={math.degrees(cyaw):+.2f}°"
            )

        # ── Execute sequence ──
        twist = Twist()
        period = 1.0 / PUBLISH_HZ

        for i, (lx, az, dur) in enumerate(SEQUENCE):
            label = "MOVE" if (lx != 0.0 or az != 0.0) else "STOP"
            self.get_logger().info(
                f"Step {i+1}/{len(SEQUENCE)}: {label}  "
                f"linear_x={lx:.2f}  angular_z={az:.2f}  dur={dur:.2f}s"
            )

            twist.linear.x = float(lx)
            twist.angular.z = float(az)

            t_end = time.monotonic() + dur
            while time.monotonic() < t_end:
                self.pub.publish(twist)
                # Spin so odom callback stays alive
                rclpy.spin_once(self, timeout_sec=0)
                time.sleep(period)

        # Ensure we end with a full stop
        twist.linear.x = 0.0
        twist.angular.z = 0.0
        self.pub.publish(twist)
        self.get_logger().info("Sequence complete – stopped.")

        # ── Wait a moment then capture end odom ──
        time.sleep(0.3)
        odom_end = self._wait_for_odom()

        # ── Report ──
        theo_dx, theo_dy, theo_dyaw = self._integrate_sequence()
        self.get_logger().info("")
        self.get_logger().info("═══ RESULTS ═══")
        self.get_logger().info(f"Theoretical  dx={theo_dx:+.4f} m   dy={theo_dy:+.4f} m   dyaw={math.degrees(theo_dyaw):+.2f}°")

        if odom_start and odom_end:
            p0 = odom_start.pose.pose.position
            p1 = odom_end.pose.pose.position
            yaw0 = self._yaw_from_quat(odom_start.pose.pose.orientation)
            yaw1 = self._yaw_from_quat(odom_end.pose.pose.orientation)
            actual_dx = p1.x - p0.x
            actual_dy = p1.y - p0.y
            actual_dyaw = yaw1 - yaw0
            # Normalize to [-pi, pi]
            actual_dyaw = math.atan2(math.sin(actual_dyaw), math.cos(actual_dyaw))
            self.get_logger().info(f"Actual odom  dx={actual_dx:+.4f} m   dy={actual_dy:+.4f} m   dyaw={math.degrees(actual_dyaw):+.2f}°")
            self.get_logger().info(f"Error        dx={actual_dx - theo_dx:+.4f} m   dy={actual_dy - theo_dy:+.4f} m   dyaw={math.degrees(actual_dyaw - theo_dyaw):+.2f}°")
            dist_err = math.hypot(actual_dx - theo_dx, actual_dy - theo_dy)
            self.get_logger().info(f"Euclidean position error: {dist_err:.4f} m")
        else:
            self.get_logger().warn("Could not compute actual odom diff (missing odom data).")


def main():
    rclpy.init()
    node = CmdVelSequence()
    try:
        node.run_sequence()
    except KeyboardInterrupt:
        # Send stop on Ctrl-C
        stop = Twist()
        node.pub.publish(stop)
        node.get_logger().info("Interrupted – stopped.")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
