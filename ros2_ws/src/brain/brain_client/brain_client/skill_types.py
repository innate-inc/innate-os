from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

import rclpy
from action_msgs.msg import GoalStatus
from brain_messages.action import ExecuteBehavior
from rclpy.action import ActionClient
from rclpy.node import Node

from brain_client.logging_config import UniversalLogger


class SkillResult(Enum):
    """
    Enum representing the possible results of a skill execution.
    """

    SUCCESS = "success"  # The skill completed successfully
    FAILURE = "failure"  # The skill failed to complete
    CANCELLED = "cancelled"  # The skill was cancelled before completion


class RobotStateType(Enum):
    """
    Enum representing the types of robot state a skill might require.
    """

    LAST_MAIN_CAMERA_IMAGE_B64 = "last_main_camera_image_b64"
    LAST_ODOM = "last_odom"
    LAST_MAP = "last_map"
    LAST_HEAD_POSITION = "last_head_position"


class InterfaceType(Enum):
    """
    Enum representing the types of interfaces a skill might require.
    """

    MANIPULATION = "manipulation"
    MOBILITY = "mobility"
    HEAD = "head"


class RobotState:
    """
    Descriptor for declaring and accessing robot state in skills.

    Usage:
        class MySkill(Skill):
            image = RobotState(RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64)
            odom = RobotState(RobotStateType.LAST_ODOM)

            def execute(self):
                if self.image:  # Access state directly
                    ...
    """

    def __init__(self, state_type: RobotStateType):
        self.state_type = state_type
        self._attr_name: str | None = None

    def __set_name__(self, owner: type, name: str):
        """Called when the descriptor is assigned to a class attribute."""
        self._attr_name = f"_robot_state_{name}"

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        """Get the current state value."""
        if obj is None:
            return self
        return getattr(obj, self._attr_name, None)

    def __set__(self, obj: Any, value: Any):
        """Set the state value."""
        setattr(obj, self._attr_name, value)


class Interface:
    """
    Descriptor for declaring and accessing interfaces in skills.

    Usage:
        class MySkill(Skill):
            mobility = Interface(InterfaceType.MOBILITY)
            head = Interface(InterfaceType.HEAD)

            def execute(self):
                self.mobility.rotate(0.5)  # Use interface directly
    """

    def __init__(self, interface_type: InterfaceType):
        self.interface_type = interface_type
        self._attr_name: str | None = None

    def __set_name__(self, owner: type, name: str):
        """Called when the descriptor is assigned to a class attribute."""
        self._attr_name = f"_interface_{name}"

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        """Get the interface instance."""
        if obj is None:
            return self
        return getattr(obj, self._attr_name, None)

    def __set__(self, obj: Any, value: Any):
        """Set the interface instance."""
        setattr(obj, self._attr_name, value)


class Skill(ABC):
    def __init__(self, logger):
        self.logger = UniversalLogger(enabled=True, wrapped_logger=logger)
        self.node: Node | None = None
        self._feedback_callback = None

    @property
    @abstractmethod
    def name(self):
        """
        The name of the skill.
        Must be defined by every subclass.
        """
        pass

    @abstractmethod
    def execute(self, *args, **kwargs):
        """
        Execute the skill.

        Subclasses must implement this method.
        Returns a tuple of (result_message, result_status) where result_status
        is a SkillResult enum value.
        """
        pass

    @abstractmethod
    def cancel(self):
        """
        Cancel the execution of the skill.

        Subclasses must implement this method to properly handle cancellation.
        This method should be safe to call at any time, even if the skill
        is not currently executing.

        Returns a message describing the cancellation result.
        """
        pass

    def update_robot_state(self, **kwargs):
        """
        Update the skill with the latest robot state.
        Automatically populates RobotState descriptors defined on the class.
        Subclasses can override this to add custom handling.
        """
        # Auto-populate RobotState descriptors
        for name, descriptor in self._get_robot_state_descriptors().items():
            state_key = descriptor.state_type.value
            if state_key in kwargs:
                setattr(self, name, kwargs[state_key])

    def get_required_robot_states(self) -> list[RobotStateType]:
        """
        Declare the robot states required by this skill.
        Automatically collects from RobotState descriptors defined on the class.
        """
        return [desc.state_type for desc in self._get_robot_state_descriptors().values()]

    def _get_robot_state_descriptors(self) -> dict[str, "RobotState"]:
        """Collect all RobotState descriptors from the class."""
        descriptors = {}
        for cls in type(self).__mro__:
            for name, attr in vars(cls).items():
                if isinstance(attr, RobotState) and name not in descriptors:
                    descriptors[name] = attr
        return descriptors

    def get_required_interfaces(self) -> list[InterfaceType]:
        """
        Declare the interfaces required by this skill.
        Automatically collects from Interface descriptors defined on the class.
        """
        return [desc.interface_type for desc in self._get_interface_descriptors().values()]

    def _get_interface_descriptors(self) -> dict[str, "Interface"]:
        """Collect all Interface descriptors from the class."""
        descriptors = {}
        for cls in type(self).__mro__:
            for name, attr in vars(cls).items():
                if isinstance(attr, Interface) and name not in descriptors:
                    descriptors[name] = attr
        return descriptors

    def inject_interface(self, interface_type: InterfaceType, interface_instance):
        """Inject an interface instance into the skill."""
        for name, descriptor in self._get_interface_descriptors().items():
            if descriptor.interface_type == interface_type:
                setattr(self, name, interface_instance)
                return True
        return False

    def guidelines(self):
        """
        Optionally provide guidelines for this skill.
        Subclasses may override this method if guidelines are available.
        """
        return None

    def guidelines_when_running(self):
        """
        Optionally provide guidelines for this skill when it is running.
        Subclasses may override this method if guidelines are available.
        """
        return None

    def set_feedback_callback(self, callback):
        """Sets the feedback callback function."""
        self._feedback_callback = callback
        self.logger.debug(f"Feedback callback set for skill {self.name}.")

    def _send_feedback(self, message: str):
        """Sends feedback if the callback is set."""
        if self._feedback_callback:
            try:
                self._feedback_callback(message)
            except Exception as e:
                self.logger.error(f"Error sending feedback for skill {self.name}: {e}")


class PhysicalSkill(Skill):
    """
    Base class for skills that execute physical behaviors via the ExecuteBehavior action server.

    Subclasses only need to define:
    - behavior_name: The name of the behavior to execute on the action server
    - display_name: Human-readable name for logging and messages
    - success_feedback_message: Message to send after successful completion
    - guidelines(): Method returning usage guidelines
    - guidelines_when_running(): Method returning runtime guidelines (optional)
    """

    def __init__(
        self,
        logger,
        behavior_name: str,
        display_name: str,
        success_feedback_message: str,
    ):
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
                   SkillResult enum value
        """
        if not self.node:
            self.logger.error(f"{self.display_name} skill is not functional due to missing ROS node.")
            return "Skill not initialized correctly (no ROS node)", SkillResult.FAILURE

        if not self._action_client:
            self._action_client = ActionClient(self.node, ExecuteBehavior, "/behavior/execute")
            if not self._action_client:
                self.logger.error(f"{self.display_name} skill could not create ExecuteBehavior action client.")
                return "Skill could not create action client", SkillResult.FAILURE

        self.logger.info(
            f" \033[96m[BrainClient] Calling ExecuteBehavior for {self.display_name.lower()} (blocking)\033[0m"
        )

        if not self._action_client.wait_for_server(timeout_sec=5.0):
            self.logger.error("ExecuteBehavior action server not available.")
            return "ExecuteBehavior action server not available", SkillResult.FAILURE

        goal_msg = ExecuteBehavior.Goal()
        goal_msg.behavior_name = self.behavior_name

        self.logger.info("Sending goal to ExecuteBehavior action server...")
        goal_future = self._action_client.send_goal_async(goal_msg, feedback_callback=self.feedback_callback)

        try:
            rclpy.spin_until_future_complete(self.node, goal_future, timeout_sec=10.0)
        except Exception as e:
            self.logger.error(f"Exception while spinning for goal future: {e}")
            return f"Failed to send goal: {e}", SkillResult.FAILURE

        if not goal_future.done():
            self.logger.error("Goal acceptance timed out.")
            return "Goal acceptance timed out", SkillResult.FAILURE

        self._goal_handle = goal_future.result()
        if not self._goal_handle.accepted:
            self.logger.info("Goal rejected by action server")
            return "Goal rejected by action server", SkillResult.FAILURE

        self.logger.info("Goal accepted by action server. Waiting for result...")
        result_future = self._goal_handle.get_result_async()

        try:
            rclpy.spin_until_future_complete(self.node, result_future, timeout_sec=60.0)
        except Exception as e:
            self.logger.error(f"Exception while spinning for result future: {e}")
            if self._goal_handle:
                self.logger.info("Attempting to cancel goal due to exception during result wait.")
                self._goal_handle.cancel_goal_async()
            return f"Failed to get result: {e}", SkillResult.FAILURE

        if not result_future.done():
            self.logger.error("Getting action result timed out.")
            if self._goal_handle:
                self.logger.info("Timing out, attempting to cancel goal.")
                self._goal_handle.cancel_goal_async()
            return f"{self.display_name} action timed out", SkillResult.FAILURE

        result_response = result_future.result()
        status = result_response.status

        action_result_message = ""
        skill_status = SkillResult.FAILURE

        self._send_feedback(self.success_feedback_message)

        if status == GoalStatus.STATUS_SUCCEEDED:
            final_result = result_response.result
            self.logger.info(f"Action succeeded! Result: {final_result.success}")
            if final_result.success:
                action_result_message = f"{self.display_name} completed successfully"
                skill_status = SkillResult.SUCCESS
            else:
                action_result_message = f"{self.display_name} action reported failure"
                skill_status = SkillResult.FAILURE
        elif status == GoalStatus.STATUS_ABORTED:
            self.logger.info("Goal aborted")
            skill_status = SkillResult.CANCELLED
            action_result_message = f"{self.display_name} aborted"
        elif status == GoalStatus.STATUS_CANCELED:
            self.logger.info("Goal canceled")
            action_result_message = f"{self.display_name} canceled"
            skill_status = SkillResult.CANCELLED
        else:
            self.logger.info(f"Goal failed with unknown status: {status}")
            action_result_message = f"{self.display_name} failed with unknown status: {status}"

        self._goal_handle = None
        return action_result_message, skill_status

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
