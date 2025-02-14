#!/usr/bin/env python3

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import PoseStamped
from std_msgs.msg import String
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
import json


class NavigationNode(Node):
    def __init__(self):
        super().__init__("navigation_node")
        self.navigator = BasicNavigator()
        self.nav_cmd_sub = self.create_subscription(
            String, "/navigation_command", self.nav_cmd_callback, 10
        )
        self.nav_result_pub = self.create_publisher(String, "/navigation_result", 10)
        self.get_logger().info("NavigationNode started and waiting for commands.")

    def nav_cmd_callback(self, msg: String):
        self.get_logger().info(f"Received navigation command: {msg.data}")
        try:
            data = json.loads(msg.data)
        except Exception as e:
            self.get_logger().error(f"Failed to decode nav command: {e}")
            return

        # Build the PoseStamped goal from the JSON data.
        goal_pose = PoseStamped()
        goal_pose.header.frame_id = data.get("frame_id", "map")
        goal_pose.header.stamp = self.get_clock().now().to_msg()
        pos = data.get("position", {"x": 0.0, "y": 0.0, "z": 0.0})
        goal_pose.pose.position.x = pos.get("x", 0.0)
        goal_pose.pose.position.y = pos.get("y", 0.0)
        goal_pose.pose.position.z = pos.get("z", 0.0)
        orient = data.get("orientation", {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0})
        goal_pose.pose.orientation.x = orient.get("x", 0.0)
        goal_pose.pose.orientation.y = orient.get("y", 0.0)
        goal_pose.pose.orientation.z = orient.get("z", 0.0)
        goal_pose.pose.orientation.w = orient.get("w", 1.0)

        self.get_logger().info(f"Navigating to: {pos}")
        # This call blocks until navigation is complete.
        self.navigator.goToPose(goal_pose)
        result = self.navigator.getResult()
        self.get_logger().info("Navigation command completed.")

        # Determine a status string based on the result.
        if result == TaskResult.SUCCEEDED:
            status = "SUCCEEDED"
        elif result == TaskResult.CANCELED:
            status = "CANCELED"
        else:
            status = "FAILED"

        # Publish the result on /navigation_result.
        result_msg = String(data=json.dumps({"status": status}))
        self.nav_result_pub.publish(result_msg)
        self.get_logger().info(f"Published navigation result: {status}")


def main(args=None):
    rclpy.init(args=args)
    nav_node = NavigationNode()
    try:
        rclpy.spin(nav_node)
    except KeyboardInterrupt:
        nav_node.get_logger().info("KeyboardInterrupt, shutting down navigation node.")
    finally:
        nav_node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
