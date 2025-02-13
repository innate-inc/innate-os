#!/usr/bin/env python3

import asyncio
import rclpy
from rclpy.duration import Duration
from rclpy.time import Time
from geometry_msgs.msg import PoseStamped, Twist
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult


async def goto_position(x, y, w):
    """
    Sends a navigation goal to navigator and waits until navigation ends.
    The function returns the TaskResult indicating whether the goal
    succeeded, was canceled, or failed/timed out.

    Args:
        goal_pose (PoseStamped): The goal pose at which you want to navigate.

    Returns:
        TaskResult: The result status from the navigator.
    """
    # Create a BasicNavigator instance to communicate with Nav2.
    navigator = BasicNavigator()

    # Create a PoseStamped goal.
    goal_pose = PoseStamped()
    goal_pose.header.frame_id = "map"
    goal_pose.header.stamp = navigator.get_clock().now().to_msg()
    goal_pose.pose.position.x = x
    goal_pose.pose.position.y = y
    goal_pose.pose.position.z = 0.0
    # Identity quaternion: no rotation.
    goal_pose.pose.orientation.x = 0.0
    goal_pose.pose.orientation.y = 0.0
    goal_pose.pose.orientation.z = 0.0
    goal_pose.pose.orientation.w = w

    print("Sending goal pose ...")
    navigator.goToPose(goal_pose)

    # Instead of a blocking call, poll asynchronously for task completion.
    # (Assuming that BasicNavigator provides a method to check whether the goal has finished.)
    # If such a method does not exist, you could check for a result in a non-blocking manner.
    while not navigator.isTaskComplete():
        await asyncio.sleep(0.1)  # Wait 100ms before checking again

    result = navigator.getResult()

    if result == TaskResult.SUCCEEDED:
        print("Goal succeeded!")
    elif result == TaskResult.CANCELED:
        print("Goal was canceled!")
    else:
        print("Goal failed or timed out.")
        print(result)

    # Option 2: Create your own node and publisher.
    stop_cmd = Twist()
    stop_cmd.linear.x = 0.0
    stop_cmd.angular.z = 0.0

    # Create a new temporary node for publishing.
    pub_node = rclpy.create_node("stop_command_node")
    cmd_vel_pub = pub_node.create_publisher(Twist, "cmd_vel", 10)

    # Publish the stop command.
    cmd_vel_pub.publish(stop_cmd)


def main():
    rclpy.init()

    navigator = BasicNavigator()

    # Create a PoseStamped for your goal
    goal_pose = PoseStamped()
    goal_pose.header.frame_id = "map"
    goal_pose.header.stamp = navigator.get_clock().now().to_msg()
    goal_pose.pose.position.x = 1.0
    goal_pose.pose.position.y = 0.0
    goal_pose.pose.position.z = 0.0
    goal_pose.pose.orientation.x = 0.0
    goal_pose.pose.orientation.y = 0.0
    goal_pose.pose.orientation.z = 0.0
    goal_pose.pose.orientation.w = (
        1.0  # This represents 0 rotation (identity quaternion)
    )

    # Send the goal
    navigator.goToPose(goal_pose)

    # Block until the result is available
    result = navigator.getResult()

    if result == TaskResult.SUCCEEDED:
        print("Goal succeeded!")
    elif result == TaskResult.CANCELED:
        print("Goal was canceled!")
    else:
        print("Goal failed or timed out.")

    rclpy.shutdown()


if __name__ == "__main__":
    main()
