#!/usr/bin/env python3
"""
Scales down cmd_vel as the robot approaches the navigation goal.

Sits between the controller_server and velocity_smoother:
  controller_server -> /cmd_vel_raw -> [this node] -> /cmd_vel_scaled -> velocity_smoother

Uses the NavigateToPose action feedback (distance_remaining) to determine
how close the robot is to the goal — no TF lookup needed.
"""

import rclpy
from rclpy.node import Node

from geometry_msgs.msg import Twist
from nav2_msgs.action import NavigateToPose
from action_msgs.msg import GoalStatusArray


class GoalApproachScaler(Node):
    def __init__(self):
        super().__init__('goal_approach_scaler')

        # Parameters
        self.declare_parameter('slowdown_radius', 0.3)      # Start slowing within this distance (m)
        self.declare_parameter('min_speed_fraction', 0.3)    # Minimum speed as fraction of commanded

        self.slowdown_radius = self.get_parameter('slowdown_radius').value
        self.min_speed_fraction = self.get_parameter('min_speed_fraction').value

        # Distance to goal from action feedback
        self.distance_remaining = float('inf')
        self.navigating = False

        # Subscribe to NavigateToPose action feedback (published by bt_navigator)
        self.feedback_sub = self.create_subscription(
            NavigateToPose.Impl.FeedbackMessage,
            '/internal_navigate_to_pose/_action/feedback',
            self._feedback_cb, 10)

        # Subscribe to action status to detect when navigation ends
        self.status_sub = self.create_subscription(
            GoalStatusArray,
            '/internal_navigate_to_pose/_action/status',
            self._status_cb, 10)

        # cmd_vel passthrough
        self.vel_sub = self.create_subscription(
            Twist, 'cmd_vel_in', self._vel_cb, 10)
        self.vel_pub = self.create_publisher(
            Twist, 'cmd_vel_out', 10)

        self.get_logger().info(
            f'Goal approach scaler: radius={self.slowdown_radius}m, '
            f'min_fraction={self.min_speed_fraction}')

    def _feedback_cb(self, msg):
        self.distance_remaining = msg.feedback.distance_remaining
        self.navigating = True

    def _status_cb(self, msg):
        """Reset when no active goals remain."""
        if not msg.status_list:
            self.navigating = False
            self.distance_remaining = float('inf')
            return
        # status 2 = EXECUTING, anything else means done/aborted/canceled
        active = any(s.status == 2 for s in msg.status_list)
        if not active:
            self.navigating = False
            self.distance_remaining = float('inf')

    def _vel_cb(self, msg: Twist):
        if not self.navigating or self.distance_remaining >= self.slowdown_radius:
            self.vel_pub.publish(msg)
            return

        # Linear ramp: at dist==0 -> min_speed_fraction, at dist==radius -> 1.0
        fraction = self.min_speed_fraction + (1.0 - self.min_speed_fraction) * (
            self.distance_remaining / self.slowdown_radius)

        scaled = Twist()
        scaled.linear.x = msg.linear.x * fraction
        scaled.linear.y = msg.linear.y
        scaled.linear.z = msg.linear.z
        scaled.angular.x = msg.angular.x
        scaled.angular.y = msg.angular.y
        scaled.angular.z = msg.angular.z
        self.vel_pub.publish(scaled)


def main(args=None):
    rclpy.init(args=args)
    node = GoalApproachScaler()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
