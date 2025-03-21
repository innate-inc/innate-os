#!/usr/bin/env python3
"""
PrimitiveExecutionActionServer

This ROS 2 node implements an action server for executing primitives.
When a goal is received (with a primitive type and its parameters encoded
as JSON), the corresponding primitive is executed.
"""

import json
import rclpy
from rclpy.node import Node
from rclpy.action import ActionServer, GoalResponse, CancelResponse

# Import the action definition – ensure that it is built and available.
from brain_messages.action import ExecutePrimitive

# Import available primitive(s) and any needed types.
from brain_client.primitives.navigate_to_position import NavigateToPosition
from brain_client.primitives.send_email import SendEmail
from brain_client.message_types import TaskType
from brain_client.primitives.types import PrimitiveResult


class PrimitiveExecutionActionServer(Node):
    def __init__(self):
        super().__init__("primitive_execution_action_server")

        # Make the available primitives accessible via a dictionary.
        # (Key is the string value from TaskType, e.g. "navigate_to_position")
        self._primitives = {
            TaskType.NAVIGATE_TO_POSITION.value: NavigateToPosition(self.get_logger()),
            TaskType.SEND_EMAIL.value: SendEmail(self.get_logger()),
        }

        # Primitives now handle cancellation internally

        self._action_server = ActionServer(
            self,
            ExecutePrimitive,
            "execute_primitive",
            execute_callback=self.execute_callback,
            goal_callback=self.goal_callback,
            cancel_callback=self.cancel_callback,
        )
        self.get_logger().debug("Primitive Execution Action Server has started.")

    def goal_callback(self, goal_request):
        self.get_logger().debug(
            f"Received goal for primitive: '{goal_request.primitive_type}'"
        )
        return GoalResponse.ACCEPT

    def cancel_callback(self, goal_handle):
        """
        Handle cancellation requests by calling the cancel method on the primitive.
        """
        self.get_logger().debug("Received cancel request.")

        try:
            # Get the primitive type from the goal handle
            primitive_type = goal_handle.request.primitive_type

            # Find and cancel the primitive
            if primitive_type in self._primitives:
                primitive = self._primitives[primitive_type]
                self.get_logger().debug(f"Canceling primitive: {primitive_type}")
                primitive.cancel()
            else:
                self.get_logger().warning(f"Unknown primitive type: {primitive_type}")
        except Exception as e:
            self.get_logger().error(f"Error in cancel_callback: {str(e)}")

            # If we couldn't determine the primitive type, try to cancel all primitives
            self.get_logger().debug("Attempting to cancel all primitives")
            for name, primitive in self._primitives.items():
                try:
                    primitive.cancel()
                except Exception as cancel_error:
                    err_msg = f"Error canceling {name}: {str(cancel_error)}"
                    self.get_logger().error(err_msg)

        return CancelResponse.ACCEPT

    def execute_callback(self, goal_handle):
        self.get_logger().debug(
            f"Executing primitive: '{goal_handle.request.primitive_type}'"
        )
        # Decode the inputs (assumed to be JSON)
        try:
            inputs = json.loads(goal_handle.request.inputs)
        except Exception as e:
            self.get_logger().error(f"Invalid JSON for inputs: {str(e)}")
            goal_handle.abort()
            return ExecutePrimitive.Result(
                success=False,
                message="Invalid inputs JSON",
                success_type=PrimitiveResult.FAILURE.value,
            )

        primitive_type = goal_handle.request.primitive_type
        if primitive_type not in self._primitives:
            self.get_logger().error(f"Primitive '{primitive_type}' not available")
            goal_handle.abort()
            return ExecutePrimitive.Result(
                success=False,
                message="Primitive not available",
                primitive_type=primitive_type,
                success_type=PrimitiveResult.FAILURE.value,
            )

        primitive = self._primitives[primitive_type]
        self.get_logger().debug(
            f"Starting primitive '{primitive_type}' with inputs: {inputs}"
        )

        try:
            # Execute the primitive
            result_message, result_status = primitive.execute(**inputs)

            # Handle the result based on the PrimitiveResult enum
            if result_status == PrimitiveResult.SUCCESS:
                self.get_logger().info(
                    f"Primitive '{primitive_type}' succeeded: {result_message}"
                )
                goal_handle.succeed()
                return ExecutePrimitive.Result(
                    success=True,
                    message=result_message,
                    primitive_type=primitive_type,
                    success_type=PrimitiveResult.SUCCESS.value,
                )
            elif result_status == PrimitiveResult.CANCELLED:
                self.get_logger().info(
                    f"Primitive '{primitive_type}' cancelled: {result_message}"
                )
                goal_handle.canceled()
                return ExecutePrimitive.Result(
                    success=True,
                    message=result_message,
                    primitive_type=primitive_type,
                    success_type=PrimitiveResult.CANCELLED.value,
                )
            else:  # PrimitiveResult.FAILURE
                self.get_logger().info(
                    f"Primitive '{primitive_type}' failed: {result_message}"
                )
                goal_handle.abort()
                return ExecutePrimitive.Result(
                    success=False,
                    message=result_message,
                    primitive_type=primitive_type,
                    success_type=PrimitiveResult.FAILURE.value,
                )

        except Exception as e:
            self.get_logger().error(f"Error executing primitive: {str(e)}")
            goal_handle.abort()
            return ExecutePrimitive.Result(
                success=False,
                message=str(e),
                primitive_type=primitive_type,
                success_type=PrimitiveResult.FAILURE.value,
            )

    def destroy(self):
        self._action_server.destroy()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    action_server = PrimitiveExecutionActionServer()
    rclpy.spin(action_server)
    action_server.destroy()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
