#!/usr/bin/env python3
import rclpy
from rclpy.action import ActionClient
from action_msgs.msg import GoalStatus  # Corrected import
from brain_messages.action import (
    ExecuteBehavior,
)  # Assuming the action file is in brain_messages/action
from brain_client.primitives.types import Primitive, PrimitiveResult

class PickUpTrash(Primitive):
    """
    Primitive for picking up trash by calling an action server.
    """

    def __init__(self, logger):
        super().__init__(logger) # Call superclass __init__
        # self.node will be set by the PrimitiveExecutionActionServer
        self._action_client = None # Initialize to None
        self._goal_handle = None
        # self.action_result = None # Can be local to execute
        # self.action_success = False # Can be local to execute

    @property
    def name(self):
        return "pick_up_trash"

    def guidelines(self):
        return (
            "To use when you see a piece of trash on the floor and it's close enough to pick up (under 1m away). "
            "If unsure if close enough, try to navigate closer before calling this primitive. "
            "For the time being, only use to pick up white pieces of paper, not another piece of trash. "
        )
    
    def guidelines_when_running(self):
        return (
            "While you are picking up the trash, watch your main and wrist camera. "
            "You have successfully picked up the trash if your wrist camera shows the trash and the arm is clearly up. "
            "If you successfully pick up a trash early, you can stop the primitive and note that you have picked up the trash. "
            "If the primitive completes, watch your wrist camera to see if you have picked up the trash successfully or not."
        )

    def feedback_callback(self, feedback_msg):
        feedback = feedback_msg.feedback
        self.logger.info(
            f"Received feedback: Elapsed Time: {feedback.elapsed_time}, Remaining Time: {feedback.remaining_time}"
        )

    def execute(self):
        """
        Executes the pick_up_trash policy by calling the ExecuteBehavior action server.
        This is a blocking call.

        Returns:
            tuple: (result_message, result_status) where result_status is a
                   PrimitiveResult enum value
        """
        if not self.node: # Check if node is set
            self.logger.error(
                "PickUpTrash primitive is not functional due to missing ROS node."
            )
            return "Primitive not initialized correctly (no ROS node)", PrimitiveResult.FAILURE

        if not self._action_client: # Initialize client if it doesn't exist
            self._action_client = ActionClient(self.node, ExecuteBehavior, "/behavior/execute")
            if not self._action_client:
                self.logger.error(
                    "PickUpTrash primitive could not create ExecuteBehavior action client."
                )
                return "Primitive could not create action client", PrimitiveResult.FAILURE

        self.logger.info(
            f" \033[96m[BrainClient] Calling ExecuteBehavior for picking up trash (blocking)\033[0m"
        )

        if not self._action_client.wait_for_server(timeout_sec=5.0):
            self.logger.error("ExecuteBehavior action server not available.")
            return "ExecuteBehavior action server not available", PrimitiveResult.FAILURE

        goal_msg = ExecuteBehavior.Goal()
        goal_msg.behavior_name = "pick_paper"

        self.logger.info("Sending goal to ExecuteBehavior action server...")
        goal_future = self._action_client.send_goal_async(
            goal_msg, feedback_callback=self.feedback_callback
        )

        try:
            rclpy.spin_until_future_complete(
                self.node, goal_future, timeout_sec=10.0
            )  # Timeout for goal acceptance
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
            )  # Timeout for goal completion
        except Exception as e:
            self.logger.error(f"Exception while spinning for result future: {e}")
            # Attempt to cancel if spinning fails for some reason before timeout
            if self._goal_handle:
                self.logger.info(
                    "Attempting to cancel goal due to exception during result wait."
                )
                self._goal_handle.cancel_goal_async()
            return f"Failed to get result: {e}", PrimitiveResult.FAILURE

        if not result_future.done():
            self.logger.error("Getting action result timed out.")
            if self._goal_handle:  # Attempt to cancel if timed out
                self.logger.info("Timing out, attempting to cancel goal.")
                self._goal_handle.cancel_goal_async()  # Fire and forget cancel
            return "Picking up trash action timed out", PrimitiveResult.FAILURE

        result_response = result_future.result()
        status = result_response.status

        action_result_message = ""
        primitive_status = PrimitiveResult.FAILURE

        # First send feedback that the robot should be looking at its wrist camera
        # to check if the trash is picked up
        self._send_feedback("I should be looking at my wrist camera to check if I have picked up the trash successfully")

        if status == GoalStatus.STATUS_SUCCEEDED:
            final_result = result_response.result
            self.logger.info(f"Action succeeded! Result: {final_result.success}")
            if final_result.success:
                action_result_message = "Trash picked up successfully"
                primitive_status = PrimitiveResult.SUCCESS
            else:
                action_result_message = "Picking up trash action reported failure"
                primitive_status = PrimitiveResult.FAILURE
        elif status == GoalStatus.STATUS_ABORTED:
            self.logger.info("Goal aborted")
            primitive_status = PrimitiveResult.CANCELLED
            action_result_message = "Picking up trash aborted"
        elif status == GoalStatus.STATUS_CANCELED:
            self.logger.info("Goal canceled")
            action_result_message = "Picking up trash canceled"
            primitive_status = PrimitiveResult.CANCELLED
        else:
            self.logger.info(f"Goal failed with unknown status: {status}")
            action_result_message = (
                f"Picking up trash failed with unknown status: {status}"
            )

        self._goal_handle = None  # Clear the handle once done
        return action_result_message, primitive_status

    def cancel(self):
        """
        Cancel the pick_up_trash operation.
        This is a best-effort cancellation.
        """
        if self._goal_handle:
            self.logger.info("\033[91m[BrainClient] Canceling pick_up_trash operation \033[0m")
            cancel_future = self._goal_handle.cancel_goal_async()
            # We could spin for cancel_future here if we need to confirm cancellation
            # For a simple cancel, just sending the request is often enough.
            # rclpy.spin_until_future_complete(self.node, cancel_future, timeout_sec=5.0)
            # self.logger.info(f"Cancel request result: {cancel_future.result()}")
            return "\033[92m[BrainClient] Cancellation request sent for picking up trash. \033[0m"
        else:
            self.logger.info(
                "\033[91m[BrainClient] Pick up trash operation cannot be canceled as no goal is active. \033[0m"
            )
            return "\033[91m[BrainClient] No active pick_up_trash operation to cancel. \033[0m"


# Note: Proper rclpy lifecycle management (init/shutdown, node creation/destruction)
# is crucial and ideally handled by the main application orchestrating primitives.
# The get_ros_node function here is a simplified approach for demonstration.
