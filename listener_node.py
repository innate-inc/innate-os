#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


class ListenerNode(Node):
    def __init__(self):
        super().__init__("listener_node")

        # Subscribe to 'genesis_sensors'
        self.subscription = self.create_subscription(
            String, "genesis_sensors", self.callback, 10
        )

    def callback(self, msg):
        self.get_logger().info(f"Received from outside: {msg.data}")


def main(args=None):
    rclpy.init(args=args)
    node = ListenerNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
