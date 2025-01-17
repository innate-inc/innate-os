#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


class GenesisBridgeNode(Node):
    def __init__(self):
        super().__init__("genesis_bridge_node")

        # Publisher that will send “sensor” data from Genesis
        self.publisher_ = self.create_publisher(String, "genesis_sensors", 10)

        # Timer to simulate reading data from Genesis.
        # In a real scenario, you’d integrate with the actual Genesis API,
        # e.g. base_pos = robot.get_pos() -> Publish on /genesis_sensors
        self.timer = self.create_timer(0.5, self.timer_callback)
        self.counter = 0

    def timer_callback(self):
        # Simulate some data from the robot’s sensors
        msg = String()
        msg.data = f"Fake sensor reading {self.counter}"
        self.counter += 1
        self.publisher_.publish(msg)
        self.get_logger().info(f"Published: {msg.data}")


def main(args=None):
    rclpy.init(args=args)
    node = GenesisBridgeNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
