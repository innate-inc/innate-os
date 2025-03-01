#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from rclpy.time import Time


class MoveForwardNode(Node):
    def __init__(self):
        super().__init__("move_forward_node")
        self.cmd_vel_pub = self.create_publisher(Twist, "/cmd_vel", 10)
        self.odom_sub = self.create_subscription(
            Odometry, "/odom", self.odom_callback, 10
        )

        self.initial_position = None
        self.start_time = None
        self.reached_goal = False

        self.timer = self.create_timer(0.1, self.publish_cmd_vel)

    def publish_cmd_vel(self):
        """Publish a forward velocity command until the robot reaches x=1.0."""
        if not self.reached_goal:
            twist = Twist()
            twist.linear.x = 1.0  # Forward at 1 m/s
            twist.angular.z = 0.0
            self.cmd_vel_pub.publish(twist)

    def odom_callback(self, msg: Odometry):
        """Track the robot's position and calculate the time to reach x=1.0."""
        # Get the x position in the odom frame
        current_x = msg.pose.pose.position.x

        self.get_logger().info(f"Current x: {current_x}")

        # Record the starting position and time
        if self.initial_position is None:
            self.initial_position = current_x
            self.start_time = self.get_clock().now()

        # Check if the robot has reached x=1.0
        if (current_x - self.initial_position) >= 1.0 and not self.reached_goal:
            self.reached_goal = True
            end_time = self.get_clock().now()
            time_taken = end_time - self.start_time

            self.get_logger().info(
                f"Reached x=1.0. Time taken: {time_taken.nanoseconds / 1e9:.2f} seconds."
            )

            # Stop the robot
            twist = Twist()
            self.cmd_vel_pub.publish(twist)

            # Shut down the node
            self.destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = MoveForwardNode()
    rclpy.spin(node)
    rclpy.shutdown()


if __name__ == "__main__":
    main()
