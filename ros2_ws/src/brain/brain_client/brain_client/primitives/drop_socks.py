#!/usr/bin/env python3
import rclpy
from rclpy.action import ActionClient
from action_msgs.msg import GoalStatus
from brain_messages.action import (
    ExecuteBehavior,
)
from brain_client.primitives.types import Primitive, PrimitiveResult

class DropSocks(Primitive):
    """
    Primitive for dropping a sock by calling an action server.
    """

    def __init__(self, logger):
        super().__init__(logger)
        self._action_client = None
        self._goal_handle = None

    @property
    def name(self):
        return "drop_socks"

    def guidelines(self):
        return (
            "To use when you are holding a sock and want to drop it in the wooden box associated for it. "
            "The wooden box has to be visible in your main camera."
        )

    def guidelines_when_running(self):
        return (
            "While you are dropping the sock, watch your main and wrist camera. "
            "You have successfully dropped the sock if your wrist camera shows your gripper is empty and the sock is in the wooden box. "
            "If you successfully drop a sock in the wooden box, you can stop the primitive. "
            "If the primitive completes, watch your wrist camera to see if you have dropped the sock successfully or not."
        )

    def feedback_callback(self, feedback_msg):
        feedback = feedback_msg.feedback
        self.logger.info(
            f"Received feedback: Elapsed Time: {feedback.elapsed_time}, Remaining Time: {feedback.remaining_time}"
        )

    def execute(self):
        """
        Executes the drop_socks policy by calling the ExecuteBehavior action server.
        This is a blocking call.

        Returns:
            tuple: (result_message, result_status) where result_status is a
                   PrimitiveResult enum value
        """
        if not self.node:
            self.logger.error(
                "DropSocks primitive is not functional due to missing ROS node."
            )
            return "Primitive not initialized correctly (no ROS node)", PrimitiveResult.FAILURE

        if not self._action_client:
            self._action_client = ActionClient(self.node, ExecuteBehavior, "/behavior/execute")
            if not self._action_client:
                self.logger.error(
                    "DropSocks primitive could not create ExecuteBehavior action client."
                )
                return "Primitive could not create action client", PrimitiveResult.FAILURE

        self.logger.info(
            f" \033[96m[BrainClient] Calling ExecuteBehavior for dropping sock (blocking)\033[0m"
        )

        if not self._action_client.wait_for_server(timeout_sec=5.0):
            self.logger.error("ExecuteBehavior action server not available.")
            return "ExecuteBehavior action server not available", PrimitiveResult.FAILURE

        goal_msg = ExecuteBehavior.Goal()
        goal_msg.behavior_name = "drop_socks"

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
            return "Dropping sock action timed out", PrimitiveResult.FAILURE

        result_response = result_future.result()
        status = result_response.status

        action_result_message = ""
        primitive_status = PrimitiveResult.FAILURE

        self._send_feedback("I should be looking at my wrist camera to check if I have dropped the sock successfully")

        if status == GoalStatus.STATUS_SUCCEEDED:
            final_result = result_response.result
            self.logger.info(f"Action succeeded! Result: {final_result.success}")
            if final_result.success:
                action_result_message = "Sock dropped successfully"
                primitive_status = PrimitiveResult.SUCCESS
            else:
                action_result_message = "Dropping sock action reported failure"
                primitive_status = PrimitiveResult.FAILURE
        elif status == GoalStatus.STATUS_ABORTED:
            self.logger.info("Goal aborted")
            primitive_status = PrimitiveResult.CANCELLED
            action_result_message = "Dropping sock aborted"
        elif status == GoalStatus.STATUS_CANCELED:
            self.logger.info("Goal canceled")
            action_result_message = "Dropping sock canceled"
            primitive_status = PrimitiveResult.CANCELLED
        else:
            self.logger.info(f"Goal failed with unknown status: {status}")
            action_result_message = (
                f"Dropping sock failed with unknown status: {status}"
            )

        self._goal_handle = None
        return action_result_message, primitive_status

    def cancel(self):
        """
        Cancel the drop_socks operation.
        This is a best-effort cancellation.
        """
        if self._goal_handle:
            self.logger.info("\033[91m[BrainClient] Canceling drop_socks operation \033[0m")
            self._goal_handle.cancel_goal_async()
            return "\033[92m[BrainClient] Cancellation request sent for dropping sock. \033[0m"
        else:
            self.logger.info(
                "\033[91m[BrainClient] Drop sock operation cannot be canceled as no goal is active. \033[0m"
            )
            return "\033[91m[BrainClient] No active drop_socks operation to cancel. \033[0m" 