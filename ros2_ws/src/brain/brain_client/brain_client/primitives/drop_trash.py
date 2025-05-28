#!/usr/bin/env python3
import time # Keep for delays between service calls if needed, but not for simulating service duration
import rclpy
from rclpy.node import Node # For type hinting, though get_ros_node creates it
from maurice_msgs.srv import GotoJS
from brain_client.primitives.types import Primitive, PrimitiveResult
import threading

_rclpy_initialized_drop_trash = False
_node_drop_trash = None
_rclpy_init_lock_drop_trash = threading.Lock()

def get_ros_node_for_drop_trash(logger):
    """
    Manages rclpy initialization and node creation for the DropTrash primitive.
    Tries to initialize rclpy if not already done and creates a single node instance.
    """
    global _rclpy_initialized_drop_trash, _node_drop_trash
    with _rclpy_init_lock_drop_trash:
        if not _rclpy_initialized_drop_trash:
            if not rclpy.ok(): # Check if rclpy is already initialized externally
                try:
                    rclpy.init()
                    logger.info("RCLPY initialized by DropTrash primitive.")
                except Exception as e:
                    logger.error(f"DropTrash: Failed to initialize rclpy: {e}")
                    return None
            else:
                logger.info("DropTrash: RCLPY was already initialized.")
            
            # Create a unique node name, or use a generic one if this node is shared
            node_name = f'drop_trash_primitive_node_{threading.get_ident()}'
            _node_drop_trash = rclpy.create_node(node_name)
            _rclpy_initialized_drop_trash = True
            logger.info(f"DropTrash: Node '{node_name}' created.")
        elif _node_drop_trash is None: # rclpy was initialized, but our node wasn't made
            node_name = f'drop_trash_primitive_node_{threading.get_ident()}'
            _node_drop_trash = rclpy.create_node(node_name)
            logger.info(f"DropTrash: Node '{node_name}' created on pre-initialized rclpy.")
        return _node_drop_trash


class DropTrash(Primitive):
    """
    Primitive for commanding the robot arm to drop trash.
    This involves a sequence of arm movements using ROS 2 services.
    """

    def __init__(self, logger):
        self.logger = logger
        self.node = get_ros_node_for_drop_trash(self.logger)
        self.goto_js_client = None

        if self.node:
            self.goto_js_client = self.node.create_client(GotoJS, '/maurice_arm/goto_js')
        else:
            self.logger.error("DropTrash primitive could not acquire a ROS node. Service client not created.")

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
        if not self.node or not self.goto_js_client:
            self.logger.error("ROS Node or GotoJS service client not initialized.")
            return False, "ROS Node or service client not initialized"

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
            rclpy.spin_until_future_complete(self.node, future, timeout_sec=time_duration + 5.0) 
        except Exception as e:
            self.logger.error(f"Exception while calling /maurice_arm/goto_js service: {e}")
            return False, f"Exception during service call: {e}"

        if future.result() is not None:
            response = future.result()
            # Log the message from the service, but don't check response.success
            self.logger.info(f"Service /maurice_arm/goto_js responded. Message: {response}")
            # Consider the call "successful" in terms of attempt and response received
            return True, response
        else:
            self.logger.error("Service call to /maurice_arm/goto_js timed out or did not receive a response.")
            return False, "Service call timed out or no response"


    def execute(self):
        """
        Executes the trash dropping sequence by making two ROS service calls.
        """
        if not self.node or not self.goto_js_client:
            self.logger.error("DropTrash primitive is not functional due to missing ROS node or service client.")
            return "Primitive not initialized correctly (no ROS node/client)", PrimitiveResult.FAILURE

        self.logger.info("\033[96m[BrainClient] Initiating drop trash sequence.\033[0m")

        # First arm movement: go to zero position
        # Assuming a 6-joint arm, all set to 0.0
        joint_positions_1 = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        time_1 = int(5.0)

        success, message = self._call_goto_js_service(joint_positions_1, time_1)
        if not success:
            return f"Failed first arm movement to zero position: {message}", PrimitiveResult.FAILURE
        
        self.logger.info("Arm movement 1 (to zero position) completed. Waiting for 5 seconds...")
        # It's important that this time.sleep happens *after* the service call has completed.
        # The rclpy.spin_until_future_complete ensures the service call itself is blocking.
        time.sleep(5.0)

        # Second arm movement: from zero position, adjust last joint
        joint_positions_2 = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0] # Start from zero position
        joint_positions_2[-1] = 0.88  # Target value for the last joint
        time_2 = int(1.0)

        time.sleep(1.0)

        success, message = self._call_goto_js_service(joint_positions_2, time_2)
        if not success:
            return f"Failed second arm movement (adjusting last joint): {message}", PrimitiveResult.FAILURE

        joint_positions_3 = [0.8528933180644165, -0.45712627478992107, 1.2946797849754812, -0.9326603190344698, -0.04908738521234052, 0.8881748761857863]
        time_3 = int(2.0)
        success, message = self._call_goto_js_service(joint_positions_3, time_3)
        if not success:
            return f"Failed third arm movement (returning to original position): {message}", PrimitiveResult.FAILURE

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
