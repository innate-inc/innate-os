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
from rclpy.executors import MultiThreadedExecutor


# Import the action definition – ensure that it is built and available.
from brain_messages.action import ExecutePrimitive

# Import available primitive(s) and any needed types.
from brain_client.primitives.navigate_to_position import NavigateToPosition
from brain_client.primitives.navigate_to_position_sim import NavigateToPositionSim
from brain_client.primitives.send_email import SendEmail
from brain_client.primitives.send_picture_via_email import SendPictureViaEmail
from brain_client.primitives.pick_up_trash import PickUpTrash
from brain_client.primitives.drop_trash import DropTrash
from brain_client.primitives.pick_up_sock import PickUpSock
from brain_client.primitives.drop_socks import DropSocks
from brain_client.primitives.play_move import PlayMove
from brain_client.primitives.get_chess_move import GetChessMove

from brain_client.primitives.types import (
    PrimitiveResult,
    RobotStateType,
)  # Import RobotStateType

from brain_client.message_types import TaskType

# Import ROS message types for subscriptions
from sensor_msgs.msg import CompressedImage, Image, JointState  # Added JointState
from nav_msgs.msg import Odometry, OccupancyGrid
from cv_bridge import CvBridge


class PrimitiveExecutionActionServer(Node):
    def __init__(self):
        super().__init__("primitive_execution_action_server")

        # Robot state storage
        self.last_main_camera_image = None  # Stores cv2 image object
        self.last_odom = None  # Stores Odometry message
        self.last_map = None  # Stores OccupancyGrid message
        self.last_ik_solution = None  # Stores JointState message from IK solver
        
        # Initialize CvBridge for raw image conversion
        self.cv_bridge = CvBridge()

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
        # Determine if we should subscribe to compressed or raw images based on topic name
        if "compressed" in self.image_topic:
            self.main_camera_image_sub = self.create_subscription(
                CompressedImage,
                self.image_topic,
                self.main_camera_image_compressed_callback,
                image_qos,
            )
            self.get_logger().info(f"Subscribing to compressed image topic: {self.image_topic}")
        else:
            self.main_camera_image_sub = self.create_subscription(
                Image,
                self.image_topic,
                self.main_camera_image_raw_callback,
                image_qos,
            )
            self.get_logger().info(f"Subscribing to raw image topic: {self.image_topic}")
        self.odom_sub = self.create_subscription(
            Odometry, "/odom", self.odom_callback, 10
        )
        self.map_sub = self.create_subscription(
            OccupancyGrid,
            "/map",
            self.map_callback,
            1,  # QoS profile with transient local durability might be better for map
        )
        self.ik_solution_sub = self.create_subscription(
            JointState,
            "ik_solution",
            self.ik_solution_callback,
            10,
        )

        # Mapping from TaskType to primitive class
        # Choose navigation primitive based on parameter
        navigation_primitive = (
            NavigateToPositionSim if self.simulator_mode else NavigateToPosition
        )
        self.get_logger().info(
            f"Using {'simulator' if self.simulator_mode else 'Nav2'} navigation primitive"
        )

        primitive_classes = {
            TaskType.NAVIGATE_TO_POSITION: navigation_primitive,
            TaskType.SEND_EMAIL: SendEmail,
            TaskType.SEND_PICTURE_VIA_EMAIL: SendPictureViaEmail,
            TaskType.PICK_UP_TRASH: PickUpTrash,
            TaskType.DROP_TRASH: DropTrash,
            TaskType.PICK_UP_SOCK: PickUpSock,
            TaskType.DROP_SOCKS: DropSocks,
            TaskType.PLAY_MOVE: PlayMove,
            TaskType.GET_CHESS_MOVE: GetChessMove,
        }

        self._primitives = {}
        for task_type, primitive_class in primitive_classes.items():
            primitive_instance = primitive_class(self.get_logger())
            primitive_instance.node = self  # Inject the node
            self._primitives[task_type.value] = primitive_instance

        self._action_server = ActionServer(
            self,
            ExecutePrimitive,
            "execute_primitive",
            execute_callback=self.execute_callback,
            goal_callback=self.goal_callback,
            cancel_callback=self.cancel_callback,
        )
        self.get_logger().info("🎯 Primitive Execution Action Server has started!")
        self.get_logger().info(f"📋 Available primitives: {list(self._primitives.keys())}")

    def goal_callback(self, goal_request):
        self.get_logger().info(
            f"🎯 GOAL RECEIVED for primitive: '{goal_request.primitive_type}'"
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
        self.get_logger().info(
            f"🎬 STARTING execution of primitive: '{goal_handle.request.primitive_type}'"
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
        self.get_logger().info(
            f"🚀 About to execute primitive '{primitive_type}' with inputs: {inputs}"
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

            if RobotStateType.LAST_IK_SOLUTION in primitive.get_required_robot_states():
                self.get_logger().debug(f"Updating primitive {primitive.name} with new IK solution.")
                if self.last_ik_solution is not None:
                    robot_state_to_inject[RobotStateType.LAST_IK_SOLUTION.value] = {
                        "header": {
                            "stamp": {
                                "sec": self.last_ik_solution.header.stamp.sec,
                                "nanosec": self.last_ik_solution.header.stamp.nanosec,
                            },
                            "frame_id": self.last_ik_solution.header.frame_id,
                        },
                        "name": self.last_ik_solution.name,
                        "position": list(self.last_ik_solution.position),
                        "velocity": list(self.last_ik_solution.velocity),
                        "effort": list(self.last_ik_solution.effort),
                    }
                else:
                    self.get_logger().warn(
                        f"Primitive {primitive_type} requires LAST_IK_SOLUTION but "
                        f"none available."
                    )

            if robot_state_to_inject:  # Only call if there is state to update
                primitive.update_robot_state(**robot_state_to_inject)

            # Execute the primitive with its direct inputs
            self.get_logger().info(f"⚡ CALLING primitive.execute() for '{primitive_type}' with inputs: {inputs}")
            result_message, result_status = primitive.execute(**inputs)
            self.get_logger().info(f"✅ primitive.execute() COMPLETED for '{primitive_type}' with result: {result_status.value}, message: {result_message}")

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
    def main_camera_image_compressed_callback(self, msg: CompressedImage):
        try:
            np_arr = np.frombuffer(msg.data, np.uint8)
            self.last_main_camera_image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            self.get_logger().debug("Received and decoded compressed image for primitives.")
        except Exception as e:
            self.get_logger().error(
                f"Failed to decode compressed image for primitive state: {e}"
            )

    def main_camera_image_raw_callback(self, msg: Image):
        try:
            self.last_main_camera_image = self.cv_bridge.imgmsg_to_cv2(msg, "bgr8")
            self.get_logger().debug("Received and converted raw image for primitives.")
        except Exception as e:
            self.get_logger().error(
                f"Failed to convert raw image for primitive state: {e}"
            )

    def odom_callback(self, msg: Odometry):
        self.last_odom = msg
        # self.get_logger().debug('Received new odometry for primitives.')

    def map_callback(self, msg: OccupancyGrid):
        self.last_map = msg
        # self.get_logger().debug('Received new map for primitives.')

    def ik_solution_callback(self, msg: JointState):
        """
        Handles incoming IK solutions and updates any running primitives
        that require this state.
        """
        self.last_ik_solution = msg
        self.get_logger().debug(f'Received IK solution: {msg.position}')

        # Find the currently active primitive. In this system, we assume one at a time.
        # A more complex system might need to iterate through a list of active goals.
        active_primitive = None
        for primitive in self._primitives.values():
            # This is a simplification; a real system might need a more robust
            # way to check if a primitive is "active".
            # For now, we assume any primitive that requires IK might be waiting for it.
            if RobotStateType.LAST_IK_SOLUTION in primitive.get_required_robot_states():
                active_primitive = primitive
                break

        if active_primitive:
            self.get_logger().debug(f"Updating primitive '{active_primitive.name}' with new IK solution.")
            ik_solution_dict = {
                "header": {
                    "stamp": {
                        "sec": msg.header.stamp.sec,
                        "nanosec": msg.header.stamp.nanosec,
                    },
                    "frame_id": msg.header.frame_id,
                },
                "name": msg.name,
                "position": list(msg.position),
                "velocity": list(msg.velocity),
                "effort": list(msg.effort),
            }
            # Directly update the primitive's state.
            active_primitive.update_robot_state(**{RobotStateType.LAST_IK_SOLUTION.value: ik_solution_dict})


def main(args=None):
    rclpy.init(args=args)
    action_server = PrimitiveExecutionActionServer()

    # A MultiThreadedExecutor is required to allow the action's execute_callback
    # to block while still allowing other callbacks (like ik_solution_callback)
    # to be processed concurrently.
    executor = MultiThreadedExecutor()
    executor.add_node(action_server)

    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        # It's good practice to explicitly destroy the node and shut down rclpy
        action_server.destroy()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
