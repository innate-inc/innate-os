import rclpy
import asyncio
import math

from geometry_msgs.msg import PoseStamped, Twist
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from brain_client.primitives.types import Primitive


class Nav2Controller:
    def __init__(self, logger):
        """
        Initialize the Nav2Controller by creating a BasicNavigator instance
        """
        # Create a BasicNavigator instance to communicate with Nav2.
        self.navigator = BasicNavigator()
        self.logger = logger

        self.logger.info("Nav2 position primitive node created")

    def go_to_position(self, x: float, y: float, theta: float):
        """
        Sends a navigation goal to the navigator and waits until navigation ends.
        The method returns the TaskResult indicating whether the goal
        succeeded, was canceled, or failed/timed out.

        Args:
            x (float): x-coordinate of the target position.
            y (float): y-coordinate of the target position.
            w (float): The w component of the orientation (no rotation implies an identity quaternion).

        Returns:
            TaskResult: The result status from the navigator.
        """
        # Create a PoseStamped goal.
        goal_pose = PoseStamped()
        goal_pose.header.frame_id = "map"
        goal_pose.header.stamp = self.navigator.get_clock().now().to_msg()
        goal_pose.pose.position.x = x
        goal_pose.pose.position.y = y
        goal_pose.pose.position.z = 0.0

        goal_pose.pose.orientation.x = 0.0
        goal_pose.pose.orientation.y = 0.0
        goal_pose.pose.orientation.z = math.sin(theta / 2.0)
        goal_pose.pose.orientation.w = math.cos(theta / 2.0)

        self.logger.debug("Sending goal pose ...")
        self.navigator.goToPose(goal_pose)

        self.logger.debug("Waiting for navigation to complete ...")

        while not self.navigator.isTaskComplete():
            feedback = self.navigator.getFeedback()

        result = self.navigator.getResult()

        if result == TaskResult.SUCCEEDED:
            self.logger.debug("Goal succeeded!")
        elif result == TaskResult.CANCELED:
            self.logger.debug("Goal was canceled!")
        else:
            self.logger.debug(f"Goal failed or timed out. result: {result}")

        # Stop the robot by publishing a stop command.
        stop_cmd = Twist()
        stop_cmd.linear.x = 0.0
        stop_cmd.angular.z = 0.0
        # self.cmd_vel_pub.publish(stop_cmd)

        return result


class NavigateToPosition(Primitive):
    def __init__(self, logger):
        self.nav2_controller = Nav2Controller(logger)
        self.logger = logger

    @property
    def name(self):
        return "navigate_to_position"

    def guidelines(self):
        return (
            "Navigate the robot to the specified position using provided x, y, and w coordinates. "
            + "Can be used to navigate to a specific point in the map, or rotate the robot, or move forward..."
        )

    def execute(self, x: float, y: float, theta: float):
        # Replace this simulated delay and print statements with actual navigation logic.
        self.logger.info(
            f"\033[96m[BrainClient] Initiating navigation to position: x={x}, y={y}, theta={theta}\033[0m"
        )

        self.nav2_controller.go_to_position(x, y, theta)

        self.logger.info(
            f"\033[92m[BrainClient] Navigation complete. Arrived at position: x={x}, y={y}, theta={theta}\033[0m"
        )
        return f"Reached position ({x}, {y}, {theta})", True
