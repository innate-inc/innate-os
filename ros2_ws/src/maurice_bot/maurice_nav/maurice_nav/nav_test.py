#!/usr/bin/env python3
"""
A simple test node that sends a navigation goal using nav2_simple_commander asynchronously.
"""

import asyncio
import traceback
import rclpy
from geometry_msgs.msg import PoseStamped, Twist
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult


async def async_main():
    rclpy.init()

    # Create a BasicNavigator instance to communicate with Nav2.
    navigator = BasicNavigator()

    # Create a PoseStamped goal.
    goal_pose = PoseStamped()
    goal_pose.header.frame_id = "map"
    goal_pose.header.stamp = navigator.get_clock().now().to_msg()
    goal_pose.pose.position.x = 0.0
    goal_pose.pose.position.y = -1.0
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

    # Option 2: Create your own node and publisher.
    stop_cmd = Twist()
    stop_cmd.linear.x = 0.0
    stop_cmd.angular.z = 0.0

    # Create a new temporary node for publishing.
    pub_node = rclpy.create_node("stop_command_node")
    cmd_vel_pub = pub_node.create_publisher(Twist, "cmd_vel", 10)

    # Publish the stop command.
    cmd_vel_pub.publish(stop_cmd)

    # Spin once to process the publishing event.
    rclpy.spin_once(pub_node, timeout_sec=0.1)

    # Clean up the publisher node.
    pub_node.destroy_node()

    rclpy.shutdown()


async def wrap_execution(f):
    """
    Wraps an awaitable (the primitive's execution coroutine) and yields status messages.

    :param coro: The awaitable (coroutine) representing the primitive execution.
    :yield: Status update dictionaries.
    """
    # Yield the "started" event
    yield {"status": "started", "message": "Execution started."}
    try:
        # Await the actual execution
        result = await f()
    except asyncio.CancelledError:
        # Yield "interrupted" event if there is a cancellation
        yield {
            "status": "interrupted",
            "message": "Execution was interrupted.",
        }
        raise
    except Exception as e:
        # Yield "failed" event if any exception is raised
        yield {
            "status": "failed",
            "message": f"Execution failed with error: {e}. Traceback: {traceback.format_exc()}",
        }
        raise
    else:
        # If everything goes fine, yield "completed" event
        yield {
            "status": "completed",
            "result": result,
        }


async def main():
    gen = wrap_execution(async_main)
    async for status in gen:
        print(status)


if __name__ == "__main__":
    asyncio.run(main())
