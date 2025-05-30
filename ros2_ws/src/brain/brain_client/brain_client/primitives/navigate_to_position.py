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
        path = self.navigator.getPath(goal_pose, goal_pose, use_start=False)

        # If the path is None, we can't navigate to the goal
        if path is None:
            self.logger.error("Failed to get path to goal")
            return TaskResult.FAILED

        self.navigator.goToPose(goal_pose)

        self.logger.debug("Waiting for navigation to complete ...")

        was_canceled = False

        # Modified loop to check for cancellation
        while not self.navigator.isTaskComplete():
            # Check if cancellation was requested
            if self._cancel_requested.is_set():
                self.logger.info("Cancellation detected in navigation loop")
                was_canceled = True
                self.navigator.cancelTask()
                break

            # Get feedback but don't block for too long
            feedback = self.navigator.getFeedback()
            if feedback:
                distance_remaining = feedback.distance_remaining
                current_pose = feedback.current_pose.pose
                current_position = current_pose.position
                goal_position = goal_pose.pose.position
                current_orientation = current_pose.orientation

                # Compute the distance to the goal
                distance_to_goal = math.sqrt(
                    (current_position.x - goal_position.x) ** 2
                    + (current_position.y - goal_position.y) ** 2
                )
                # Compute the angle to the goal
                goal_orientation = math.atan2(
                    goal_position.y - current_position.y,
                    goal_position.x - current_position.x,
                )

                # Calculate current robot yaw from quaternion
                # Assuming current_orientation has x, y, z, w attributes
                q_x = current_orientation.x
                q_y = current_orientation.y
                q_z = current_orientation.z
                q_w = current_orientation.w
                current_robot_yaw = math.atan2(
                    2.0 * (q_w * q_z + q_x * q_y), 1.0 - 2.0 * (q_y * q_y + q_z * q_z)
                )

                angle_difference = math.atan2(
                    math.sin(goal_orientation - current_robot_yaw),
                    math.cos(goal_orientation - current_robot_yaw),
                )

                # Now we compute 2 percentages:
                # 1. The percentage of the path that has been completed
                # 2. The percentage of the angle that has been completed
                path_completion = distance_remaining / distance_to_goal * 100
                angle_completion = abs(angle_difference) / math.pi * 100

                # Compute the
                self.logger.info(
                    f"Path completed: {path_completion}%. Angle completed: {angle_completion}%. Average completion: {(path_completion + angle_completion) / 2}%"
                )

                # Here we should basically decide that if we're close enough to the goal, we should send the feedback to the server
                # It probably is relative to the distance to the goal that was computed by the navigator
                # Also the angle matters. And it's badly computed right now.

                # If we haven't moved a lot in the past 10 seconds, we should also send that feedback to the server
                # saying that we're stuck and another primitive should be used to get us unstuck.
                # Although it's usually the goal of the navigator to get us unstuck...
                # Let's just say if we're stuck for a while, we need to change the goal.

                # One thing to consider that could be good though is, if we're close enough to the goal,
                # we can say "I'm close enough to the goal, if I need to navigate again, I should already start doing it now while stopping the current task"
                # That should really be if we're 95% of the way there, because if what we wanted was a small movement, we don't want to cancel the task.

            # Small sleep to prevent CPU hogging
            time.sleep(0.1)  # 100ms check interval

        result = self.navigator.getResult()

        if was_canceled:
            self.logger.debug("Goal was canceled!")
            # This should not be necessary but somehow the navigator.cancelTask does not make result == TaskResult.CANCELED
            result = TaskResult.CANCELED
        elif result == TaskResult.SUCCEEDED:
            self.logger.debug("Goal succeeded!")
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
            f"Initiating navigation to position: x={x}, y={y}, theta={theta}"
        )

        result = self.nav2_controller.go_to_position(x, y, theta)

        # Check if the navigation was canceled
        if result == TaskResult.CANCELED:
            self.logger.info("Navigation was canceled")
            return "Navigation canceled", PrimitiveResult.CANCELLED
        elif result == TaskResult.SUCCEEDED:
            self.logger.info(
                f"Navigation complete. Arrived at position: x={x}, y={y}, theta={theta}"
            )
            return f"Reached position ({x}, {y}, {theta})", PrimitiveResult.SUCCESS
        else:
            self.logger.info(f"Navigation failed with result: {result}")
            return f"Navigation failed with result: {result}", PrimitiveResult.FAILURE

    def cancel(self):
        """
        Cancels the current navigation task.
        """
        self.logger.debug("Canceling navigation task")
        self.nav2_controller.cancel_navigation()
        return "Navigation canceled"
