#!/usr/bin/env python3
"""
A simple test node that sends a navigation goal using nav2_simple_commander asynchronously.
"""

import asyncio
import rclpy
from geometry_msgs.msg import PoseStamped
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult


async def async_main():
    rclpy.init()

    # Create a BasicNavigator instance to communicate with Nav2.
    navigator = BasicNavigator()

    # Create a PoseStamped goal.
    goal_pose = PoseStamped()
    goal_pose.header.frame_id = "map"
    goal_pose.header.stamp = navigator.get_clock().now().to_msg()
    goal_pose.pose.position.x = 1.0
    goal_pose.pose.position.y = 0.0
    goal_pose.pose.position.z = 0.0
    # Identity quaternion: no rotation.
    goal_pose.pose.orientation.x = 0.0
    goal_pose.pose.orientation.y = 0.0
    goal_pose.pose.orientation.z = 0.0
    goal_pose.pose.orientation.w = 1.0

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

    rclpy.shutdown()


if __name__ == "__main__":
    asyncio.run(async_main())
