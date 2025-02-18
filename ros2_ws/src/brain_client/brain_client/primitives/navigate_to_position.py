import rclpy
import asyncio

from geometry_msgs.msg import PoseStamped, Twist
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult
from brain_client.primitives.types import Primitive


class Nav2Controller:
    def __init__(self, logger):
        """
        Initialize the Nav2Controller by creating a BasicNavigator instance
        and preparing a ROS node with a publisher for stop commands.
        """
        # Create a BasicNavigator instance to communicate with Nav2.
        self.navigator = BasicNavigator()
        self.logger = logger

        # Create a node for publishing stop commands.
        # (Note: In a more integrated application you might want to reuse an existing node.)
        # self.pub_node = rclpy.create_node("navigate_to_position_stop_command_node")
        self.logger.info("Nav2 position primitive node created")
        # self.cmd_vel_pub = self.pub_node.create_publisher(Twist, "cmd_vel", 10)

    def go_to_position(self, x: float, y: float, w: float):
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

        # Identity quaternion: no rotation.
        goal_pose.pose.orientation.x = 0.0
        goal_pose.pose.orientation.y = 0.0
        goal_pose.pose.orientation.z = 0.0
        goal_pose.pose.orientation.w = w

        self.logger.info("Sending goal pose ...")
        self.navigator.goToPose(goal_pose)

        self.logger.info("Waiting for navigation to complete ...")

        while not self.navigator.isTaskComplete():
            feedback = self.navigator.getFeedback()

        result = self.navigator.getResult()

        if result == TaskResult.SUCCEEDED:
            self.logger.info("Goal succeeded!")
        elif result == TaskResult.CANCELED:
            self.logger.info("Goal was canceled!")
        else:
            self.logger.info("Goal failed or timed out.")
            self.logger.info(f"result: {result}")

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
        return "Navigate the robot to the specified position using provided x and y coordinates."

    def execute(self, x: float, y: float):
        # Replace this simulated delay and print statements with actual navigation logic.
        self.logger.info(f"Initiating navigation to position: x={x}, y={y}")

        self.nav2_controller.go_to_position(x, y, 1.0)

        self.logger.info(f"Navigation complete. Arrived at position: x={x}, y={y}")
        return f"Reached position ({x}, {y})", True
