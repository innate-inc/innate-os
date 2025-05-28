#!/usr/bin/env python3
from brain_client.primitives.types import Primitive, PrimitiveResult
import rclpy
from rclpy.action import ActionClient
from action_msgs.msg import GoalStatus # Corrected import
from brain_messages.action import ExecutePolicy # Assuming the action file is in brain_messages/action
import threading # Added import for threading

_rclpy_initialized = False
_node = None
_rclpy_init_lock = threading.Lock()

def get_ros_node(logger):
    global _rclpy_initialized, _node
    with _rclpy_init_lock:
        if not _rclpy_initialized:
            if not rclpy.ok(): # Check if rclpy is already initialized externally
                try:
                    rclpy.init()
                    logger.info("RCLPY initialized by PickUpTrash primitive.")
                except Exception as e:
                    logger.error(f"Failed to initialize rclpy: {e}")
                    return None 
            else:
                logger.info("RCLPY was already initialized.")
            
            node_name = f'pick_up_trash_primitive_node_{threading.get_ident()}'
            _node = rclpy.create_node(node_name, allow_undeclared_parameters=True, automatically_declare_parameters_from_overrides=True)
            _rclpy_initialized = True 
            logger.info(f"Node '{node_name}' created for PickUpTrash primitive.")
        elif _node is None: 
            node_name = f'pick_up_trash_primitive_node_{threading.get_ident()}'
            _node = rclpy.create_node(node_name, allow_undeclared_parameters=True, automatically_declare_parameters_from_overrides=True)
            logger.info(f"Node '{node_name}' created for PickUpTrash primitive on pre-initialized rclpy.")
        return _node

class PickUpTrash(Primitive):
    """
    Primitive for picking up trash by calling an action server.
    """

    def __init__(self, logger):
        self.logger = logger
        self.node = get_ros_node(self.logger)
        if self.node is None:
            # This will make the primitive non-functional, execute should handle this
            self.logger.error("PickUpTrash primitive could not acquire a ROS node.")
            self._action_client = None
        else:
            self._action_client = ActionClient(self.node, ExecutePolicy, 'execute_policy')
        self._goal_handle = None
        # self.action_result = None # Can be local to execute
        # self.action_success = False # Can be local to execute


    @property
    def name(self):
        return "pick_up_trash"

    def guidelines(self):
        return (
            "Use to pick up trash by triggering the execute_policy action. "
            "To use when you see a piece of trash on the floor. "
            "Right now you can pick up white pieces of paper."
        )

    def feedback_callback(self, feedback_msg):
        feedback = feedback_msg.feedback
        self.logger.info(f'Received feedback: Elapsed Time: {feedback.elapsed_time}, Remaining Time: {feedback.remaining_time}')


    def execute(self):
        """
        Executes the pick_up_trash policy by calling the ExecutePolicy action server.
        This is a blocking call.

        Returns:
            tuple: (result_message, result_status) where result_status is a
                   PrimitiveResult enum value
        """
        if self.node is None or self._action_client is None:
            self.logger.error("PickUpTrash primitive is not functional due to missing ROS node or action client.")
            return "Primitive not initialized correctly", PrimitiveResult.FAILURE

        self.logger.info(
            f" [96m[BrainClient] Calling ExecutePolicy for picking up trash (blocking) [0m"
        )

        if not self._action_client.wait_for_server(timeout_sec=5.0):
            self.logger.error('ExecutePolicy action server not available.')
            return "ExecutePolicy action server not available", PrimitiveResult.FAILURE

        goal_msg = ExecutePolicy.Goal()
        # The goal is empty as per ExecutePolicy.action

        self.logger.info("Sending goal to ExecutePolicy action server...")
        goal_future = self._action_client.send_goal_async(
            goal_msg,
            feedback_callback=self.feedback_callback)

        try:
            rclpy.spin_until_future_complete(self.node, goal_future, timeout_sec=10.0) # Timeout for goal acceptance
        except Exception as e:
            self.logger.error(f"Exception while spinning for goal future: {e}")
            return f"Failed to send goal: {e}", PrimitiveResult.FAILURE

        if not goal_future.done():
            self.logger.error("Goal acceptance timed out.")
            return "Goal acceptance timed out", PrimitiveResult.FAILURE
            
        self._goal_handle = goal_future.result()
        if not self._goal_handle.accepted:
            self.logger.info('Goal rejected by action server')
            return "Goal rejected by action server", PrimitiveResult.FAILURE

        self.logger.info('Goal accepted by action server. Waiting for result...')
        result_future = self._goal_handle.get_result_async()

        try:
            rclpy.spin_until_future_complete(self.node, result_future, timeout_sec=60.0) # Timeout for goal completion
        except Exception as e:
            self.logger.error(f"Exception while spinning for result future: {e}")
            # Attempt to cancel if spinning fails for some reason before timeout
            if self._goal_handle:
                self.logger.info("Attempting to cancel goal due to exception during result wait.")
                self._goal_handle.cancel_goal_async()
            return f"Failed to get result: {e}", PrimitiveResult.FAILURE

        if not result_future.done():
            self.logger.error("Getting action result timed out.")
            if self._goal_handle: # Attempt to cancel if timed out
                self.logger.info("Timing out, attempting to cancel goal.")
                self._goal_handle.cancel_goal_async() # Fire and forget cancel
            return "Picking up trash action timed out", PrimitiveResult.FAILURE

        result_response = result_future.result()
        status = result_response.status
        
        action_result_message = ""
        primitive_status = PrimitiveResult.FAILURE

        if status == GoalStatus.STATUS_SUCCEEDED:
            final_result = result_response.result
            self.logger.info(f'Action succeeded! Result: {final_result.success}')
            if final_result.success:
                action_result_message = "Trash picked up successfully"
                primitive_status = PrimitiveResult.SUCCESS
            else:
                action_result_message = "Picking up trash action reported failure"
                primitive_status = PrimitiveResult.FAILURE
        elif status == GoalStatus.STATUS_ABORTED:
            self.logger.info('Goal aborted')
            action_result_message = "Picking up trash aborted"
        elif status == GoalStatus.STATUS_CANCELED:
            self.logger.info('Goal canceled')
            action_result_message = "Picking up trash canceled"
        else:
            self.logger.info(f'Goal failed with unknown status: {status}')
            action_result_message = f"Picking up trash failed with unknown status: {status}"
        
        self._goal_handle = None # Clear the handle once done
        return action_result_message, primitive_status


    def cancel(self):
        """
        Cancel the pick_up_trash operation.
        This is a best-effort cancellation.
        """
        if self._goal_handle and self._goal_handle.is_active:
            self.logger.info(" [91m[BrainClient] Canceling pick_up_trash operation [0m")
            cancel_future = self._goal_handle.cancel_goal_async()
            # We could spin for cancel_future here if we need to confirm cancellation
            # For a simple cancel, just sending the request is often enough.
            # rclpy.spin_until_future_complete(self.node, cancel_future, timeout_sec=5.0)
            # self.logger.info(f"Cancel request result: {cancel_future.result()}")
            return "Cancellation request sent for picking up trash."
        else:
            self.logger.info(
                " [91m[BrainClient] Pick up trash operation cannot be canceled as no goal is active. [0m"
            )
            return "No active pick_up_trash operation to cancel."

# Note: Proper rclpy lifecycle management (init/shutdown, node creation/destruction)
# is crucial and ideally handled by the main application orchestrating primitives.
# The get_ros_node function here is a simplified approach for demonstration.
