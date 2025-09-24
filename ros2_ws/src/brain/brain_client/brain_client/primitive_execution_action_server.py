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
import cv2  # For image processing
import base64  # For encoding
import numpy as np  # For map data
import math  # For yaw calculation

from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy


# Import the action definition – ensure that it is built and available.
from brain_messages.action import ExecutePrimitive

# Import primitive loader and types
from brain_client.primitive_loader import PrimitiveLoader
from brain_client.primitive_types import (
    PrimitiveResult,
    RobotStateType,
)
from brain_client.message_types import TaskType

# Special imports for primitives that need specific handling
from brain_client.primitives.navigate_to_position import NavigateToPosition
from brain_client.primitives.navigate_to_position_sim import NavigateToPositionSim

# Import ROS message types for subscriptions
from sensor_msgs.msg import CompressedImage  # Image removed as it is unused
from nav_msgs.msg import Odometry, OccupancyGrid


class PrimitiveExecutionActionServer(Node):
    def __init__(self):
        super().__init__("primitive_execution_action_server")

        # Robot state storage
        self.last_main_camera_image = None  # Stores cv2 image object
        self.last_odom = None  # Stores Odometry message
        self.last_map = None  # Stores OccupancyGrid message

        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
        )

        self.declare_parameter("image_topic", "/camera/color/image_raw/compressed")
        self.image_topic = self.get_parameter("image_topic").value

        self.declare_parameter("simulator_mode", False)
        self.simulator_mode = self.get_parameter("simulator_mode").value

        # Subscribers for robot state
        # TODO: Make topic names configurable if needed (e.g., via parameters)
        self.main_camera_image_sub = self.create_subscription(
            CompressedImage,
            self.image_topic,
            self.main_camera_image_callback,
            image_qos,
        )
        self.odom_sub = self.create_subscription(
            Odometry, "/odom", self.odom_callback, 10
        )
        self.map_sub = self.create_subscription(
            OccupancyGrid,
            "/map",
            self.map_callback,
            1,  # QoS profile with transient local durability might be better for map
        )

        # Dynamic primitive loading
        self.primitive_loader = PrimitiveLoader(self.get_logger())
        
        # Define directory to scan for primitives
        import os
        base_dir = os.path.dirname(os.path.abspath(__file__))
        primitives_directory = os.path.join(base_dir, "primitives")
        
        # Load all primitives dynamically
        discovered_primitives = self.primitive_loader.discover_primitives_in_directory(primitives_directory)
        
        # Handle special case for navigation primitive based on simulator mode
        navigation_primitive = (
            NavigateToPositionSim if self.simulator_mode else NavigateToPosition
        )
        self.get_logger().info(
            f"Using {'simulator' if self.simulator_mode else 'Nav2'} navigation primitive"
        )
        
        # Override the navigation primitive if it was discovered
        if "navigate_to_position" in discovered_primitives:
            discovered_primitives["navigate_to_position"] = navigation_primitive

        # Create primitive instances
        self._primitives = {}
        for primitive_name, primitive_class in discovered_primitives.items():
            try:
                primitive_instance = primitive_class(self.get_logger())
                primitive_instance.node = self  # Inject the node
                self._primitives[primitive_name] = primitive_instance
                self.get_logger().debug(f"Loaded primitive: {primitive_name}")
            except Exception as e:
                self.get_logger().error(f"Error instantiating primitive {primitive_name}: {e}")
        
        self.get_logger().info(f"Successfully loaded {len(self._primitives)} primitives")

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
        self.get_logger().info("Received cancel request.")

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

        # Define a feedback publisher for the primitive
        def _publish_feedback(update_message: str):
            try:
                # Assuming ExecutePrimitive.Feedback is the correct type
                # and it has a string field.
                feedback_msg = ExecutePrimitive.Feedback()
                feedback_msg.feedback = update_message

                goal_handle.publish_feedback(feedback_msg)
                self.get_logger().debug(
                    f"Published feedback for '{primitive_type}': {update_message}; feedback_msg: {feedback_msg}"
                )
            except Exception as e:
                self.get_logger().error(
                    f"Failed to publish feedback for '{primitive_type}': {e}"
                )

        # Pass the feedback callback to the primitive if it supports it
        primitive.set_feedback_callback(_publish_feedback)

        try:
            # Get required states and inject them into the primitive
            required_states = primitive.get_required_robot_states()
            robot_state_to_inject = {}

            if RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64 in required_states:
                if self.last_main_camera_image is not None:
                    try:
                        encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 70]
                        success, encoded_img_bytes = cv2.imencode(
                            ".jpg", self.last_main_camera_image, encode_params
                        )
                        if success:
                            robot_state_to_inject[
                                RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64.value
                            ] = base64.b64encode(encoded_img_bytes.tobytes()).decode(
                                "utf-8"
                            )
                        else:
                            self.get_logger().error(
                                "Failed to encode last_main_camera_image for primitive state"
                            )
                    except Exception as e_img:
                        self.get_logger().error(
                            f"Error encoding last_main_camera_image for primitive: {e_img}"
                        )
                else:
                    self.get_logger().warn(
                        f"Primitive {primitive_type} requires "
                        f"LAST_MAIN_CAMERA_IMAGE_B64 but none available."
                    )

            if RobotStateType.LAST_ODOM in required_states:
                if self.last_odom is not None:
                    pos = self.last_odom.pose.pose.position
                    ori = self.last_odom.pose.pose.orientation
                    siny_cosp = 2.0 * (ori.w * ori.z + ori.x * ori.y)
                    cosy_cosp = 1.0 - 2.0 * (ori.y * ori.y + ori.z * ori.z)
                    theta = math.atan2(siny_cosp, cosy_cosp)
                    robot_state_to_inject[RobotStateType.LAST_ODOM.value] = {
                        "header": {
                            "stamp": {
                                "sec": self.last_odom.header.stamp.sec,
                                "nanosec": self.last_odom.header.stamp.nanosec,
                            },
                            "frame_id": self.last_odom.header.frame_id,
                        },
                        "child_frame_id": self.last_odom.child_frame_id,
                        "pose": {
                            "pose": {
                                "position": {"x": pos.x, "y": pos.y, "z": pos.z},
                                "orientation": {
                                    "x": ori.x,
                                    "y": ori.y,
                                    "z": ori.z,
                                    "w": ori.w,
                                },
                            }
                        },
                        "theta_degrees": math.degrees(theta),
                    }
                else:
                    self.get_logger().warn(
                        f"Primitive {primitive_type} requires LAST_ODOM "
                        f"but none available."
                    )

            if RobotStateType.LAST_MAP in required_states:
                if self.last_map is not None:
                    map_data_bytes = np.array(
                        self.last_map.data, dtype=np.int8
                    ).tobytes()
                    ori_map = self.last_map.info.origin.orientation
                    siny_cosp_map = 2.0 * (
                        ori_map.w * ori_map.z + ori_map.x * ori_map.y
                    )
                    cosy_cosp_map = 1.0 - 2.0 * (
                        ori_map.y * ori_map.y + ori_map.z * ori_map.z
                    )
                    yaw_map = math.atan2(siny_cosp_map, cosy_cosp_map)
                    robot_state_to_inject[RobotStateType.LAST_MAP.value] = {
                        "header": {
                            "stamp": {
                                "sec": self.last_map.header.stamp.sec,
                                "nanosec": self.last_map.header.stamp.nanosec,
                            },
                            "frame_id": self.last_map.header.frame_id,
                        },
                        "info": {
                            "map_load_time": {
                                "sec": self.last_map.info.map_load_time.sec,
                                "nanosec": self.last_map.info.map_load_time.nanosec,
                            },
                            "resolution": self.last_map.info.resolution,
                            "width": self.last_map.info.width,
                            "height": self.last_map.info.height,
                            "origin": {
                                "position": {
                                    "x": self.last_map.info.origin.position.x,
                                    "y": self.last_map.info.origin.position.y,
                                    "z": self.last_map.info.origin.position.z,
                                },
                                "orientation": {
                                    "x": ori_map.x,
                                    "y": ori_map.y,
                                    "z": ori_map.z,
                                    "w": ori_map.w,
                                },
                                "yaw_degrees": math.degrees(yaw_map),
                            },
                        },
                        "data_b64": base64.b64encode(map_data_bytes).decode("utf-8"),
                    }
                else:
                    self.get_logger().warn(
                        f"Primitive {primitive_type} requires LAST_MAP but "
                        f"none available."
                    )

            if robot_state_to_inject:  # Only call if there is state to update
                primitive.update_robot_state(**robot_state_to_inject)

            # Execute the primitive with its direct inputs
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
                goal_handle.succeed()
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

    # Callbacks for state subscriptions
    def main_camera_image_callback(self, msg: CompressedImage):
        try:
            np_arr = np.frombuffer(msg.data, np.uint8)
            self.last_main_camera_image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            self.get_logger().debug("Received and decoded new image for primitives.")
        except Exception as e:
            self.get_logger().error(
                f"Failed to decode compressed image for primitive state: {e}"
            )

    def odom_callback(self, msg: Odometry):
        self.last_odom = msg
        # self.get_logger().debug('Received new odometry for primitives.')

    def map_callback(self, msg: OccupancyGrid):
        self.last_map = msg
        # self.get_logger().debug('Received new map for primitives.')


def main(args=None):
    rclpy.init(args=args)
    action_server = PrimitiveExecutionActionServer()
    rclpy.spin(action_server)
    action_server.destroy()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
