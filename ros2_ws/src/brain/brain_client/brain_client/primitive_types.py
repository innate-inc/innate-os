from abc import ABC, abstractmethod
from enum import Enum
import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from action_msgs.msg import GoalStatus
from brain_messages.action import ExecuteBehavior
from brain_client.logging_config import UniversalLogger


class PrimitiveResult(Enum):
    """
    Enum representing the possible results of a primitive execution.
    """

    SUCCESS = "success"  # The primitive completed successfully
    FAILURE = "failure"  # The primitive failed to complete
    CANCELLED = "cancelled"  # The primitive was cancelled before completion


class RobotStateType(Enum):
    """
    Enum representing the types of robot state a primitive might require.
    """
    LAST_MAIN_CAMERA_IMAGE_B64 = "last_main_camera_image_b64"
    LAST_ODOM = "last_odom"
    LAST_MAP = "last_map"
    LAST_HEAD_POSITION = "last_head_position"


class Primitive(ABC):
    def __init__(self, logger):
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)
        self.node: Node | None = None
        self.manipulation = None  # Will be injected by primitive_execution_action_server
        self.mobility = None  # Will be injected by primitive_execution_action_server
        self.head = None  # Will be injected by primitive_execution_action_server
        self._feedback_callback = None

    @property
    @abstractmethod
    def name(self):
        """
        The name of the primitive.
        Must be defined by every subclass.
        """
        pass

    @abstractmethod
    def execute(self, *args, **kwargs):
        """
        Execute the primitive.

        Subclasses must implement this method.
        Returns a tuple of (result_message, result_status) where result_status
        is a PrimitiveResult enum value.
        """
        pass

    @abstractmethod
    def cancel(self):
        """
        Cancel the execution of the primitive.

        Subclasses must implement this method to properly handle cancellation.
        This method should be safe to call at any time, even if the primitive
        is not currently executing.

        Returns a message describing the cancellation result.
        """
        pass

    def update_robot_state(self, **kwargs):
        """
        Update the primitive with the latest robot state.
        Subclasses can override this to store relevant data.
        Example: self.last_image = kwargs.get('last_image_b64')
        """
        pass

    def get_required_robot_states(self) -> list[RobotStateType]:
        """
        Declare the robot states required by this primitive.
        Subclasses should override this to specify their needs.
        Returns a list of RobotStateType enum members.
        """
        return []

    def guidelines(self):
        """
        Optionally provide guidelines for this primitive.
        Subclasses may override this method if guidelines are available.
        """
        return None
    
    def guidelines_when_running(self):
        """
        Optionally provide guidelines for this primitive when it is running.
        Subclasses may override this method if guidelines are available.
        """
        return None

    def set_feedback_callback(self, callback):
        """Sets the feedback callback function."""
        self._feedback_callback = callback
        self.logger.debug(f"Feedback callback set for primitive {self.name}.")

    def _send_feedback(self, message: str):
        """Sends feedback if the callback is set."""
        if self._feedback_callback:
            try:
                self._feedback_callback(message)
            except Exception as e:
                self.logger.error(
                    f"Error sending feedback for primitive {self.name}: {e}"
                )


class PhysicalPrimitive(Primitive):
    """
    Base class for primitives that execute physical behaviors via the ExecuteBehavior action server.
    
    Subclasses only need to define:
    - behavior_name: The name of the behavior to execute on the action server
    - display_name: Human-readable name for logging and messages
    - success_feedback_message: Message to send after successful completion
    - guidelines(): Method returning usage guidelines
    - guidelines_when_running(): Method returning runtime guidelines (optional)
    """
    
    def __init__(self, logger, behavior_name: str, display_name: str, success_feedback_message: str):
        super().__init__(logger)
        self._action_client = None
        self._goal_handle = None
        self.behavior_name = behavior_name
        self.display_name = display_name
        self.success_feedback_message = success_feedback_message

    def feedback_callback(self, feedback_msg):
        feedback = feedback_msg.feedback
        self.logger.info(
            f"Received feedback: Elapsed Time: {feedback.elapsed_time}, Remaining Time: {feedback.remaining_time}"
        )

    def execute(self):
        """
        Executes the physical behavior by calling the ExecuteBehavior action server.
        This is a blocking call.

        Returns:
            tuple: (result_message, result_status) where result_status is a
                   PrimitiveResult enum value
        """
        if not self.node:
            self.logger.error(
                f"{self.display_name} primitive is not functional due to missing ROS node."
            )
            return "Primitive not initialized correctly (no ROS node)", PrimitiveResult.FAILURE

        if not self._action_client:
            self._action_client = ActionClient(self.node, ExecuteBehavior, "/behavior/execute")
            if not self._action_client:
                self.logger.error(
                    f"{self.display_name} primitive could not create ExecuteBehavior action client."
                )
                return "Primitive could not create action client", PrimitiveResult.FAILURE

        self.logger.info(
            f" \033[96m[BrainClient] Calling ExecuteBehavior for {self.display_name.lower()} (blocking)\033[0m"
        )

        if not self._action_client.wait_for_server(timeout_sec=5.0):
            self.logger.error("ExecuteBehavior action server not available.")
            return "ExecuteBehavior action server not available", PrimitiveResult.FAILURE

        goal_msg = ExecuteBehavior.Goal()
        goal_msg.behavior_name = self.behavior_name

        self.logger.info("Sending goal to ExecuteBehavior action server...")
        goal_future = self._action_client.send_goal_async(
            goal_msg, feedback_callback=self.feedback_callback
        )

        try:
            rclpy.spin_until_future_complete(
                self.node, goal_future, timeout_sec=10.0
            )
        except Exception as e:
            self.logger.error(f"Exception while spinning for goal future: {e}")
            return f"Failed to send goal: {e}", PrimitiveResult.FAILURE

        if not goal_future.done():
            self.logger.error("Goal acceptance timed out.")
            return "Goal acceptance timed out", PrimitiveResult.FAILURE

        self._goal_handle = goal_future.result()
        if not self._goal_handle.accepted:
            self.logger.info("Goal rejected by action server")
            return "Goal rejected by action server", PrimitiveResult.FAILURE

        self.logger.info("Goal accepted by action server. Waiting for result...")
        result_future = self._goal_handle.get_result_async()

        try:
            rclpy.spin_until_future_complete(
                self.node, result_future, timeout_sec=60.0
            )
        except Exception as e:
            self.logger.error(f"Exception while spinning for result future: {e}")
            if self._goal_handle:
                self.logger.info(
                    "Attempting to cancel goal due to exception during result wait."
                )
                self._goal_handle.cancel_goal_async()
            return f"Failed to get result: {e}", PrimitiveResult.FAILURE

        if not result_future.done():
            self.logger.error("Getting action result timed out.")
            if self._goal_handle:
                self.logger.info("Timing out, attempting to cancel goal.")
                self._goal_handle.cancel_goal_async()
            return f"{self.display_name} action timed out", PrimitiveResult.FAILURE

        result_response = result_future.result()
        status = result_response.status

        action_result_message = ""
        primitive_status = PrimitiveResult.FAILURE

        self._send_feedback(self.success_feedback_message)

        if status == GoalStatus.STATUS_SUCCEEDED:
            final_result = result_response.result
            self.logger.info(f"Action succeeded! Result: {final_result.success}")
            if final_result.success:
                action_result_message = f"{self.display_name} completed successfully"
                primitive_status = PrimitiveResult.SUCCESS
            else:
                action_result_message = f"{self.display_name} action reported failure"
                primitive_status = PrimitiveResult.FAILURE
        elif status == GoalStatus.STATUS_ABORTED:
            self.logger.info("Goal aborted")
            primitive_status = PrimitiveResult.CANCELLED
            action_result_message = f"{self.display_name} aborted"
        elif status == GoalStatus.STATUS_CANCELED:
            self.logger.info("Goal canceled")
            action_result_message = f"{self.display_name} canceled"
            primitive_status = PrimitiveResult.CANCELLED
        else:
            self.logger.info(f"Goal failed with unknown status: {status}")
            action_result_message = (
                f"{self.display_name} failed with unknown status: {status}"
            )

        self._goal_handle = None
        return action_result_message, primitive_status

    def cancel(self):
        """
        Cancel the physical behavior operation.
        This is a best-effort cancellation.
        """
        if self._goal_handle:
            self.logger.info(f"\033[91m[BrainClient] Canceling {self.display_name.lower()} operation \033[0m")
            self._goal_handle.cancel_goal_async()
            return f"\033[92m[BrainClient] Cancellation request sent for {self.display_name.lower()}. \033[0m"
        else:
            self.logger.info(
                f"\033[91m[BrainClient] {self.display_name} operation cannot be canceled as no goal is active. \033[0m"
            )
            return f"\033[91m[BrainClient] No active {self.display_name.lower()} operation to cancel. \033[0m"
