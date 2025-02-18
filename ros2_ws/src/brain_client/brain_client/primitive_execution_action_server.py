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
from brain_client.message_types import TaskType


class PrimitiveExecutionActionServer(Node):
    def __init__(self):
        super().__init__("primitive_execution_action_server")

        # Make the available primitives accessible via a dictionary.
        # (Key is the string value from TaskType, e.g. "navigate_to_position")
        self._primitives = {
            TaskType.NAVIGATE_TO_POSITION.value: NavigateToPosition(self.get_logger()),
        }

        self._action_server = ActionServer(
            self,
            ExecutePrimitive,
            "execute_primitive",
            execute_callback=self.execute_callback,
            goal_callback=self.goal_callback,
            cancel_callback=self.cancel_callback,
        )
        self.get_logger().info("Primitive Execution Action Server has started.")

    def goal_callback(self, goal_request):
        self.get_logger().info(
            'Received goal for primitive: "%s"' % goal_request.primitive_type
        )
        return GoalResponse.ACCEPT

    def cancel_callback(self, goal_handle):
        self.get_logger().info("Received cancel request.")
        return CancelResponse.ACCEPT

    def execute_callback(self, goal_handle):
        self.get_logger().info(
            'Executing primitive: "%s"' % goal_handle.request.primitive_type
        )
        # Decode the inputs (assumed to be JSON)
        try:
            inputs = json.loads(goal_handle.request.inputs)
        except Exception as e:
            self.get_logger().error("Invalid JSON for inputs: %s" % str(e))
            goal_handle.abort()
            return ExecutePrimitive.Result(success=False, message="Invalid inputs JSON")

        primitive_type = goal_handle.request.primitive_type
        if primitive_type not in self._primitives:
            self.get_logger().error('Primitive "%s" not available' % primitive_type)
            goal_handle.abort()
            return ExecutePrimitive.Result(
                success=False, message="Primitive not available"
            )

        primitive = self._primitives[primitive_type]
        self.get_logger().info(
            'Starting primitive "%s" with inputs: %s' % (primitive_type, inputs)
        )

        try:
            # Execute the primitive. (This call may block; if you want non-blocking
            # behavior you could run this in a separate thread.)
            primitive.execute(**inputs)
        except Exception as e:
            self.get_logger().error("Error executing primitive: %s" % str(e))
            goal_handle.abort()
            return ExecutePrimitive.Result(success=False, message=str(e))

        goal_handle.succeed()
        self.get_logger().info('Primitive "%s" executed successfully.' % primitive_type)
        return ExecutePrimitive.Result(
            success=True, message="Primitive executed successfully"
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
