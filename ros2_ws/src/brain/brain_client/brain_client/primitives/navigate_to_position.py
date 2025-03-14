import math
import threading
import time
from geometry_msgs.msg import PoseStamped, Twist
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from brain_client.primitives.types import Primitive, PrimitiveResult


class Nav2Controller:
    def __init__(self, logger):
        """
        Initialize the Nav2Controller by creating a BasicNavigator instance
        """
        # Create a BasicNavigator instance to communicate with Nav2.
        self.navigator = BasicNavigator()
        self.logger = logger
        # Add a cancellation flag
        self._cancel_requested = threading.Event()

        # Create a publisher for velocity commands
        # self.cmd_vel_pub = self.navigator.create_publisher(
        #     Twist, '/cmd_vel', 10
        # )

        self.logger.info("Nav2 position primitive node created")

    def go_to_position(self, x: float, y: float, theta: float):
        """
        Sends a navigation goal to the navigator and waits until navigation ends.
        The method returns the TaskResult indicating whether the goal
        succeeded, was canceled, or failed/timed out.

        Args:
            x (float): x-coordinate of the target position.
            y (float): y-coordinate of the target position.
            theta (float): The orientation angle in radians.

        Returns:
            TaskResult: The result status from the navigator.
        """
        # Reset cancellation flag
        self._cancel_requested.clear()

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

        # Modified loop to check for cancellation
        while not self.navigator.isTaskComplete():
            # Check if cancellation was requested
            if self._cancel_requested.is_set():
                self.logger.info("Cancellation detected in navigation loop")
                self.navigator.cancelTask()
                break

            # Get feedback but don't block for too long
            self.navigator.getFeedback()
            # Small sleep to prevent CPU hogging
            time.sleep(0.1)  # 100ms check interval

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

    def cancel_navigation(self):
        """
        Cancels the current navigation task.
        """
        self.logger.debug("Canceling current navigation task...")
        # Set the cancellation flag
        self._cancel_requested.set()
        # Also call cancelTask directly in case we're not in the loop
        self.navigator.cancelTask()


class NavigateToPosition(Primitive):
    def __init__(self, logger):
        self.nav2_controller = Nav2Controller(logger)
        self.logger = logger

    @property
    def name(self):
        return "navigate_to_position"

    def guidelines(self):
        return (
            "Use when you need to navigate the robot to the specified position "
            "using provided x, y coordinates, and theta (yaw) angle IN RADIANS. "
            "Can be used to navigate to a specific point in the map."
        )

    def execute(self, x: float, y: float, theta: float):
        self.logger.info(
            f"\033[96m[BrainClient] Initiating navigation to position: "
            f"x={x}, y={y}, theta={theta}\033[0m"
        )

        result = self.nav2_controller.go_to_position(x, y, theta)

        # Check if the navigation was canceled
        if result == TaskResult.CANCELED:
            self.logger.debug("\033[93m[BrainClient] Navigation was canceled\033[0m")
            return "Navigation canceled", PrimitiveResult.CANCELLED
        elif result == TaskResult.SUCCEEDED:
            self.logger.info(
                f"\033[92m[BrainClient] Navigation complete. Arrived at position: "
                f"x={x}, y={y}, theta={theta}\033[0m"
            )
            return f"Reached position ({x}, {y}, {theta})", PrimitiveResult.SUCCESS
        else:
            self.logger.info(
                f"\033[91m[BrainClient] Navigation failed with result: {result}\033[0m"
            )
            return f"Navigation failed with result: {result}", PrimitiveResult.FAILURE

    def cancel(self):
        """
        Cancels the current navigation task.
        """
        self.logger.debug("\033[91m[BrainClient] Canceling navigation task\033[0m")
        self.nav2_controller.cancel_navigation()
        return "Navigation canceled"
