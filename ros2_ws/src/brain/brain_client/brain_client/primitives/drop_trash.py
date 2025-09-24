#!/usr/bin/env python3
import time  # Keep for delays between service calls if needed, but not for simulating service duration
import rclpy
from rclpy.node import Node  # For type hinting, though get_ros_node creates it
from maurice_msgs.srv import GotoJS
from brain_client.primitive_types import Primitive, PrimitiveResult
import threading


class DropTrash(Primitive):
    """
    Primitive for commanding the robot arm to drop trash.
    This involves a sequence of arm movements using ROS 2 services.
    """

    def __init__(self, logger):
        super().__init__(logger)  # Call superclass __init__ without node
        # self.node will be set by the PrimitiveExecutionActionServer
        self.goto_js_client = None

        # Client creation will need to be moved or delayed until node is set.
        # For now, let's assume it's handled by checking self.node before use,
        # or by moving client creation to a method called after node is set.
        # The current structure in execute() already checks if self.node and self.goto_js_client exist.

    @property
    def name(self):
        return "drop_trash"

    def guidelines(self):
        return (
            "Use this command to drop trash that you are holding. "
            "Use this when in the area you want to drop the trash in."
        )

    def _call_goto_js_service(self, joint_positions, time_duration):
        """Helper function to call the /maurice_arm/goto_js service."""
        if not self.node:
            self.logger.error("ROS Node not available for GotoJS service client.")
            return False, "ROS Node not available"

        if not self.goto_js_client:  # Initialize client if it doesn't exist
            self.goto_js_client = self.node.create_client(
                GotoJS, "/maurice_arm/goto_js"
            )

        if not self.goto_js_client:
            self.logger.error("GotoJS service client could not be initialized.")
            return False, "GotoJS service client not initialized"

        if not self.goto_js_client.wait_for_service(timeout_sec=2.0):
            self.logger.error("Service /maurice_arm/goto_js not available.")
            return False, "Service /maurice_arm/goto_js not available"

        request = GotoJS.Request()
        request.data.data = joint_positions
        request.time = time_duration

        self.logger.info(
            f"Calling /maurice_arm/goto_js with data: {joint_positions}, time: {time_duration}s"
        )
        future = self.goto_js_client.call_async(request)

        try:
            # Spin until the future is complete or timeout occurs
            rclpy.spin_until_future_complete(
                self.node, future, timeout_sec=time_duration + 5.0
            )
        except Exception as e:
            self.logger.error(
                f"Exception while calling /maurice_arm/goto_js service: {e}"
            )
            return False, f"Exception during service call: {e}"

        if future.result() is not None:
            response = future.result()
            # Log the message from the service, but don't check response.success
            self.logger.info(
                f"Service /maurice_arm/goto_js responded. Message: {response}"
            )
            # Consider the call "successful" in terms of attempt and response received
            return True, response
        else:
            self.logger.error(
                "Service call to /maurice_arm/goto_js timed out or did not receive a response."
            )
            return False, "Service call timed out or no response"

    def execute(self):
        """
        Executes the trash dropping sequence by making two ROS service calls.
        """
        if not self.node:  # Check if node is set
            self.logger.error(
                "DropTrash primitive is not functional due to missing ROS node."
            )
            return (
                "Primitive not initialized correctly (no ROS node)",
                PrimitiveResult.FAILURE,
            )

        # Initialize client here if not already done, now that we know self.node exists
        if not self.goto_js_client:
            self.goto_js_client = self.node.create_client(
                GotoJS, "/maurice_arm/goto_js"
            )
            if not self.goto_js_client:
                self.logger.error(
                    "DropTrash primitive could not create GotoJS service client."
                )
                return (
                    "Primitive could not create service client",
                    PrimitiveResult.FAILURE,
                )

        self.logger.info("\033[96m[BrainClient] Initiating drop trash sequence.\033[0m")

        # First arm movement: go to zero position
        # Assuming a 6-joint arm, all set to 0.0
        joint_positions_1 = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        time_1: float = 5.0

        success, message = self._call_goto_js_service(joint_positions_1, int(time_1))
        if not success:
            return (
                f"Failed first arm movement to zero position: {message}",
                PrimitiveResult.FAILURE,
            )

        self.logger.info(
            "Arm movement 1 (to zero position) completed. Waiting for 5 seconds..."
        )
        # It's important that this time.sleep happens *after* the service call has completed.
        # The rclpy.spin_until_future_complete ensures the service call itself is blocking.
        time.sleep(time_1)
        self._send_feedback("Arm ready to drop object. Dropping now...")  # FEEDBACK 1

        # Second arm movement: from zero position, adjust last joint
        joint_positions_2 = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]  # Start from zero position
        joint_positions_2[-1] = 0.88  # Target value for the last joint
        time_2: float = 2.0

        time.sleep(time_2)
        self._send_feedback(
            "Object dropped, arm returning to original position..."
        )  # FEEDBACK 2

        success, message = self._call_goto_js_service(joint_positions_2, int(time_2))
        if not success:
            return (
                f"Failed second arm movement (adjusting last joint): {message}",
                PrimitiveResult.FAILURE,
            )

        joint_positions_3 = [
            0.8528933180644165,
            -0.45712627478992107,
            1.2946797849754812,
            -0.9326603190344698,
            -0.04908738521234052,
            0.8881748761857863,
        ]
        time_3: float = 2.0
        success, message = self._call_goto_js_service(joint_positions_3, int(time_3))
        if not success:
            return (
                f"Failed third arm movement (returning to original position): {message}",
                PrimitiveResult.FAILURE,
            )

        time.sleep(time_3)

        self.logger.info(
            "\033[92m[BrainClient] Drop trash sequence completed successfully.\033[0m"
        )

        return "Trash drop sequence completed.", PrimitiveResult.SUCCESS

    def cancel(self):
        """
        Cancel the drop trash operation.
        For ROS services, true cancellation of an in-progress call is typically not supported
        by the service itself once the request is sent. This is a placeholder.
        """
        self.logger.info(
            "\033[91m[BrainClient] Drop trash operation cancellation requested.\033[0m"
        )
        # ROS services, unlike actions, don't have a built-in cancellation mechanism
        # once the call is made. If the service execution is quick, cancellation is less critical.
        # If it were a long-running service, the service itself would need to be designed
        # to periodically check for a cancellation signal (e.g., via a separate topic/service).
        return (
            "Drop trash service calls are typically atomic and cannot be directly canceled "
            "once initiated. The operation will complete or timeout."
        )


# Note on maurice_msgs.srv.GotoJS:
# This implementation assumes the GotoJS.srv definition has a request structure like:
#   float64[] data  # or a custom message type like Float64MultiArray
#   float64 time
# ---
#   bool success
#   string message
# You'll need to adjust the request population (request.data.data) and response handling
# (response.success, response.message) if your .srv definition is different.
