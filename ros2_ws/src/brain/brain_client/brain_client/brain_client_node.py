#!/usr/bin/env python3
import traceback
import rclpy
from rclpy.node import Node
from rclpy.duration import Duration
from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy
import threading
import json
import time
import cv2
import base64
import numpy as np
from rclpy.action import ActionClient
import math
import inspect
import types
import typing

# TF2 imports
# import tf2_ros # Reverted by user, then identified as unused by linter
from tf2_ros import TransformException
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener

# from tf2_geometry_msgs import do_transform_pose # Reverted by user, then identified as unused by linter
# from geometry_msgs.msg import PoseStamped # Reverted by user, then identified as unused by linter

from brain_client.message_types import (
    InternalMessage,
    InternalMessageType,
    MessageIn,
    MessageInType,
    MessageOutType,
    MessageOut,
    TaskType,
    VisionAgentOutput,
)
from brain_client.primitives.types import PrimitiveResult
from sensor_msgs.msg import CompressedImage
from geometry_msgs.msg import Twist
from std_msgs.msg import String
from sensor_msgs.msg import Image
from nav_msgs.msg import Odometry, OccupancyGrid
from brain_messages.srv import GetChatHistory
from brain_messages.action import ExecutePrimitive
from brain_messages.srv import GetAvailableDirectives
from brain_messages.srv import ResetBrain
from std_srvs.srv import SetBool

from brain_client.ws_bridge import WSBridge

from brain_client.primitives.navigate_to_position import NavigateToPosition
from brain_client.primitives.send_email import SendEmail
from brain_client.primitives.send_picture_via_email import SendPictureViaEmail
from brain_client.primitives.pick_up_trash import PickUpTrash
from brain_client.primitives.drop_trash import DropTrash

from brain_client.directives.default_directive import DefaultDirective
from brain_client.directives.sassy_directive import SassyDirective
from brain_client.directives.friendly_guide_directive import FriendlyGuideDirective
from brain_client.directives.elder_safety_directive import ElderSafetyDirective
from brain_client.directives.house_joker_directive import HouseJokerDirective
from brain_client.directives.interior_designer_directive import (
    InteriorDesignerDirective,
)
from brain_client.directives.security_patrol_directive import SecurityPatrolDirective
from brain_client.directives.clean_house_directive import CleanHouseDirective
from brain_client.directives.hide_and_seek_directive import HideAndSeekDirective

class BrainClientNode(Node):
    def __init__(self):
        super().__init__("brain_client_node")

        # Parameters
        self.declare_parameter("websocket_uri", "ws://localhost:8765")
        self.declare_parameter("token", "MY_HARDCODED_TOKEN")
        self.declare_parameter("image_topic", "/camera/color/image_raw/compressed")
        self.declare_parameter("cmd_vel_topic", "/cmd_vel")

        # New parameters for optional depth processing:
        self.declare_parameter("depth_image_topic", "/camera/depth/image_raw")

        # New parameter for the map topic
        self.declare_parameter("map_topic", "/map")

        # Set to True if you wish to receive and forward depth images as well
        self.declare_parameter("send_depth", True)
        # self.declare_parameter("odom_topic", "/odom") # Removed odom_topic

        # New parameters for camera FOV
        self.declare_parameter("vertical_fov", 60.0)
        self.declare_parameter("horizontal_resolution", 640)
        self.declare_parameter("vertical_resolution", 480)

        # New parameter for pose image interval
        self.declare_parameter("pose_image_interval", 0.5)  # 0.5 seconds default

        # Parameter for logging complete vision agent output
        self.declare_parameter("log_everything", False)  # Default to False

        self.ws_uri = (
            self.get_parameter("websocket_uri").get_parameter_value().string_value
        )
        self.token = self.get_parameter("token").get_parameter_value().string_value
        self.image_topic = (
            self.get_parameter("image_topic").get_parameter_value().string_value
        )
        self.cmd_vel_topic = (
            self.get_parameter("cmd_vel_topic").get_parameter_value().string_value
        )
        self.depth_image_topic = (
            self.get_parameter("depth_image_topic").get_parameter_value().string_value
        )
        self.map_topic = (
            self.get_parameter("map_topic").get_parameter_value().string_value
        )
        self.send_depth = (
            self.get_parameter("send_depth").get_parameter_value().bool_value
        )
        # self.odom_topic = (
        #     self.get_parameter("odom_topic").get_parameter_value().string_value
        # ) # Removed odom_topic
        self.last_odom = None
        # self.odom_sub = self.create_subscription(
        #     Odometry, self.odom_topic, self.odom_callback, 10
        # ) # Removed odom_sub

        # Create a timer to fetch the transform at 30 Hz
        self.transform_timer = self.create_timer(
            1.0 / 30.0, self.fetch_transform_callback
        )

        self.vertical_fov = (
            self.get_parameter("vertical_fov").get_parameter_value().double_value
        )
        self.horizontal_resolution = (
            self.get_parameter("horizontal_resolution")
            .get_parameter_value()
            .integer_value
        )
        self.vertical_resolution = (
            self.get_parameter("vertical_resolution")
            .get_parameter_value()
            .integer_value
        )
        self.horizontal_fov = math.degrees(
            2
            * math.atan(
                math.tan(math.radians(self.vertical_fov) / 2)
                * self.horizontal_resolution
                / self.vertical_resolution
            )
        )

        # Get pose image interval parameter
        self.pose_image_interval = (
            self.get_parameter("pose_image_interval").get_parameter_value().double_value
        )

        # Flag for logging complete vision agent output
        self.log_everything = (
            self.get_parameter("log_everything").get_parameter_value().bool_value
        )

        self.get_logger().info(f"Starting BrainClientNode with ws_uri={self.ws_uri}")
        self.get_logger().info(f"Log everything mode: {self.log_everything}")

        # Initialize TF2 buffer and listener
        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)

        # Publishers, Subscribers, and Service
        self.last_image = None
        self.last_depth_image = None
        self.last_map = None  # Store the latest map data

        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
        )

        # RGB image subscription remains unchanged.
        self.image_sub = self.create_subscription(
            CompressedImage, self.image_topic, self.image_callback, image_qos
        )

        # Subscribe to the map topic
        self.map_sub = self.create_subscription(
            OccupancyGrid, self.map_topic, self.map_callback, 1
        )

        # Optionally subscribe to the depth image topic if required.
        if self.send_depth:
            # Assuming that the depth image is published as a sensor_msgs/Image
            # (which contains: header, height, width, encoding, is_bigendian, step, data)

            self.depth_image_sub = self.create_subscription(
                Image, self.depth_image_topic, self.depth_image_callback, 1
            )

        self.cmd_vel_pub = self.create_publisher(Twist, self.cmd_vel_topic, 10)

        self.chat_history = []
        self.chat_in_sub = self.create_subscription(
            String, "/chat_in", self.chat_in_callback, 10
        )
        self.chat_out_pub = self.create_publisher(String, "/chat_out", 10)
        self.get_chat_history_srv = self.create_service(
            GetChatHistory, "/get_chat_history", self.handle_get_chat_history
        )

        # Create service for setting logging configuration
        self.set_logging_srv = self.create_service(
            SetBool, "/set_logging_config", self.handle_set_logging_config
        )

        # Create service for resetting the brain
        self.reset_srv = self.create_service(
            ResetBrain, "/reset_brain", self.handle_reset_brain
        )

        self.exit_event = threading.Event()
        self.ready_for_image = False

        # New variables for pose image timer
        self.pose_image_timer = None
        self.pose_image_started = False

        self.agent_timer = self.create_timer(0.1, self.agent_loop_callback)

        self.ws_bridge = WSBridge(
            self, incoming_topic="ws_messages", outgoing_topic="ws_outgoing"
        )
        self.ws_bridge.register_handler(
            MessageOutType.READY_FOR_IMAGE, self._handle_ready_for_image
        )
        self.ws_bridge.register_handler(
            MessageOutType.VISION_AGENT_OUTPUT, self._handle_vision_agent_output
        )
        self.ws_bridge.register_handler(MessageOutType.CHAT_OUT, self._handle_chat_out)
        self.ws_bridge.register_handler(
            MessageOutType.PRIMITIVES_AND_DIRECTIVE_REGISTERED,
            self._handle_primitives_and_directive_registered,
        )

        for _ in range(10):
            self.ws_bridge.send_message(
                InternalMessage(type=InternalMessageType.READY_FOR_CONNECTION)
            )
            time.sleep(1.0)

        # Initialize primitives dictionary
        self.primitives_dict = {
            TaskType.NAVIGATE_TO_POSITION.value: NavigateToPosition(self.get_logger()),
            TaskType.SEND_EMAIL.value: SendEmail(self.get_logger()),
            TaskType.SEND_PICTURE_VIA_EMAIL.value: SendPictureViaEmail(
                self.get_logger()
            ),
            TaskType.PICK_UP_TRASH.value: PickUpTrash(self.get_logger()),
            TaskType.DROP_TRASH.value: DropTrash(self.get_logger()),
            # Add other primitives here as they become available
        }

        self.primitive_running = None
        # Add a variable to store the current goal handle
        self._goal_handle = None

        # Add a subscription to change directive
        self.directive_sub = self.create_subscription(
            String, "/set_directive", self.set_directive_callback, 10
        )
        self.directives = {
            directive.name: directive
            for directive in [
                DefaultDirective(),
                SassyDirective(),
                FriendlyGuideDirective(),
                SecurityPatrolDirective(),
                InteriorDesignerDirective(),
                ElderSafetyDirective(),
                HouseJokerDirective(),
                CleanHouseDirective(),
                HideAndSeekDirective(),
            ]
        }
        self.current_directive = self.directives["default_directive"]

        # Create service to get available directives
        self.get_directives_srv = self.create_service(
            GetAvailableDirectives,
            "/get_available_directives",
            self.handle_get_available_directives,
        )

        # Create the primitive execution action client once in the init.
        self.primitive_action_client = ActionClient(
            self, ExecutePrimitive, "execute_primitive"
        )

        # Flag to keep track of primitive registration status
        self.primitives_registered = False
        self._pending_next_task = None

        # After initializing the primitive_action_client
        # Register the primitives with the server
        self.register_primitives_and_directive()

        self.get_logger().info(
            "\033[1;92m[BrainClient] BrainClientNode initialized\033[0m"
        )

    def chat_in_callback(self, msg: String):
        chat_entry = {"sender": "user", "text": msg.data, "timestamp": time.time()}
        self.chat_history.append(chat_entry)
        self.get_logger().info(f"\033[1;92mReceived chat_in: {chat_entry}\033[0m")
        outgoing_msg = MessageIn(type=MessageInType.CHAT_IN, payload={"text": msg.data})
        self.ws_bridge.send_message(outgoing_msg)

    def handle_get_chat_history(self, request, response):
        self.get_logger().debug(
            f"\033[1;94mReceived get_chat_history request. History: {self.chat_history}\033[0m"
        )
        response.history = json.dumps(self.chat_history)
        return response

    def handle_set_logging_config(self, request, response):
        """
        Service handler for setting the logging configuration.
        Sets the log_everything flag based on the request data.
        """
        self.log_everything = request.data
        self.get_logger().info(
            f"\033[1;92m[BrainClient] Set logging configuration: "
            f"log_everything={self.log_everything}\033[0m"
        )
        response.success = True
        response.message = (
            f"Logging configuration set: log_everything={self.log_everything}"
        )
        return response

    def image_callback(self, msg: CompressedImage):
        try:
            # self.get_logger().info(f"\033[1;92m[BrainClient] Received image_callback\033[0m")
            np_arr = np.frombuffer(msg.data, np.uint8)
            self.last_image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        except Exception as e:
            self.get_logger().error(f"Failed to decode compressed image: {e}")

    def depth_image_callback(self, msg):
        try:
            # This callback assumes a sensor_msgs/Image message for the depth channel.
            # Determine the correct numpy dtype based on the image encoding.
            if msg.encoding in ["16UC1", "mono16"]:
                dtype = np.uint16
            elif msg.encoding in ["32FC1", "mono32"]:
                dtype = np.float32
            else:
                # Fallback to uint8 if the encoding is unexpected.
                self.get_logger().warn(
                    f"Unexpected depth image encoding: {msg.encoding}, "
                    f"defaulting to uint8"
                )
                dtype = np.uint8
            depth_array = np.frombuffer(msg.data, dtype=dtype)
            depth_array = depth_array.reshape((msg.height, msg.width))
            self.last_depth_image = depth_array
        except Exception as e:
            self.get_logger().error(f"Failed to decode depth image: {e}")

    def fetch_transform_callback(self):
        try:
            robot_base_frame = "base_link"  # The frame whose pose we want
            map_frame = "map"  # The frame in which we want the pose expressed
            when = rclpy.time.Time()

            if self.tf_buffer.can_transform(
                map_frame,  # Target frame ("map")
                robot_base_frame,  # Source frame ("base_link")
                when,
                timeout=Duration(seconds=0.1),  # Short timeout for can_transform
            ):
                transform_stamped = self.tf_buffer.lookup_transform(
                    map_frame,  # Target frame ("map")
                    robot_base_frame,  # Source frame ("base_link")
                    when,
                    timeout=Duration(
                        seconds=0.1
                    ),  # Shorter timeout as can_transform likely passed
                )

                # Create an Odometry message to store the pose (or a simpler structure if preferred)
                # For now, let's try to populate an Odometry message similarly to before,
                # but using the transform directly.
                # This part might need adjustment based on how self.last_odom is used elsewhere.
                odom_msg = Odometry()
                odom_msg.header.stamp = (
                    self.get_clock().now().to_msg()
                )  # Use current time for the header
                odom_msg.header.frame_id = map_frame  # "map"
                odom_msg.child_frame_id = robot_base_frame  # "base_link"

                odom_msg.pose.pose.position.x = (
                    transform_stamped.transform.translation.x
                )
                odom_msg.pose.pose.position.y = (
                    transform_stamped.transform.translation.y
                )
                odom_msg.pose.pose.position.z = (
                    transform_stamped.transform.translation.z
                )
                odom_msg.pose.pose.orientation = transform_stamped.transform.rotation
                # Covariance and Twist are not directly available from lookup_transform
                # and might need to be handled differently or zeroed out if not critical.
                # For simplicity, let's zero them or leave them default for now.

                self.last_odom = odom_msg

                # Calculate yaw (theta) from quaternion - this theta_degrees is not used here
                ori = odom_msg.pose.pose.orientation
                siny_cosp = 2.0 * (ori.w * ori.z + ori.x * ori.y)
                cosy_cosp = 1.0 - 2.0 * (ori.y * ori.y + ori.z * ori.z)
                theta_radians = math.atan2(siny_cosp, cosy_cosp)
                theta_degrees = math.degrees(theta_radians)
            else:
                self.get_logger().warn(
                    f"Could not get transform from '{robot_base_frame}' to "
                    f"{map_frame}' at time {when.nanoseconds / 1e9:.3f}s. Waiting..."
                )
        except TransformException as ex:
            # Adjusted error message to reflect the intended transformation
            self.get_logger().error(
                f"TransformException looking up transform from "
                f"'{robot_base_frame}' to '{map_frame}': {ex}"
            )
        except Exception as e:
            self.get_logger().error(
                f"Error in fetch_transform_callback: {e}, " f"{traceback.format_exc()}"
            )

    def map_callback(self, msg: OccupancyGrid):
        """Store the latest map data."""
        self.last_map = msg

    def _handle_ready_for_image(self, msg):
        self.get_logger().info("Received READY_FOR_IMAGE; setting flag.")
        self.ready_for_image = True

        # Start the pose image timer after the first ready_for_image, if not already started
        if not self.pose_image_started and self.primitives_registered:
            self.get_logger().info("Starting regular pose image transmission")
            self.pose_image_started = True
            self.pose_image_timer = self.create_timer(
                self.pose_image_interval, self.pose_image_callback
            )

    def pose_image_callback(self):
        """Send pose images regularly with the robot's current position."""
        try:
            # Skip if no valid image or odometry data is available
            if self.last_image is None or self.last_odom is None:
                return

            # Compress the image as JPEG
            encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 70]
            success, encoded_img = cv2.imencode(".jpg", self.last_image, encode_params)
            if not success:
                self.get_logger().error("Failed to encode image for pose_image")
                return

            # Extract position and orientation data
            pos = self.last_odom.pose.pose.position
            ori = self.last_odom.pose.pose.orientation

            # Compute yaw from quaternion
            siny_cosp = 2.0 * (ori.w * ori.z + ori.x * ori.y)
            cosy_cosp = 1.0 - 2.0 * (ori.y * ori.y + ori.z * ori.z)
            theta = math.atan2(siny_cosp, cosy_cosp)

            # Create and send the pose_image message
            # No user_token is needed as the server will use the connection_id
            payload = {
                "image": base64.b64encode(encoded_img.tobytes()).decode("utf-8"),
                "x": pos.x,
                "y": pos.y,
                "theta": theta,
            }

            pose_image_msg = MessageIn(type=MessageInType.POSE_IMAGE, payload=payload)
            self.ws_bridge.send_message(pose_image_msg)
            self.get_logger().debug(
                f"Sent pose_image at position ({pos.x:.2f}, {pos.y:.2f}, {theta:.2f})"
            )
        except Exception as e:
            self.get_logger().error(f"Error in pose_image_callback: {e}")

    def _handle_vision_agent_output(self, msg):
        try:
            self.get_logger().info("[BrainClient] Received VisionAgentOutput")

            if not self.primitives_registered:
                self.get_logger().warn(
                    "\033[93m[BrainClient] Primitives not registered. Skipping VisionAgentOutput.\033[0m"
                )
                return

            # HOTFIX: If there's a msg.payload["next_task"] with a "name" field, replace it with "type".
            if "next_task" in msg.payload and msg.payload["next_task"] is not None:
                if "name" in msg.payload["next_task"]:
                    msg.payload["next_task"]["type"] = msg.payload["next_task"]["name"]
                    msg.payload["next_task"].pop("name", None)

            payload = VisionAgentOutput.model_validate(msg.payload)

            # If log_everything is enabled, send the complete vision agent output as a chat message
            if self.log_everything:
                # Convert the complete payload to a JSON string for the chat message
                complete_output_text = json.dumps(msg.payload)
                chat_entry = {
                    "sender": "vision_agent_output",
                    "text": complete_output_text,
                    "timestamp": time.time(),
                }
                self.chat_history.append(chat_entry)
                out_msg = String(data=json.dumps(chat_entry))
                self.chat_out_pub.publish(out_msg)

            self.handle_vision_agent_output(payload)
        except Exception as e:
            self.get_logger().error(
                f"Error processing vision output: {e}. Traceback: {traceback.format_exc()}"
            )

    def _handle_chat_out(self, msg, sender="robot"):
        text = msg.payload.get("text", "")
        chat_entry = {"sender": sender, "text": text, "timestamp": time.time()}
        self.chat_history.append(chat_entry)
        self.get_logger().debug(f"Received chat_out: {chat_entry}")
        out_msg = String(data=json.dumps(chat_entry))
        self.chat_out_pub.publish(out_msg)

    def handle_vision_agent_output(self, payload: VisionAgentOutput):
        execute_next_task_immediately = True

        if payload.stop_current_task:
            self.get_logger().info("\033[91m[BrainClient] Stop signal received.\033[0m")

            # Cancel the current goal if it exists
            if self._goal_handle is not None:
                self.get_logger().info(
                    "\033[91m[BrainClient] Canceling current goal.\033[0m"
                )

                # Store the next task if it exists, prevent immediate execution
                if payload.next_task is not None:
                    self.get_logger().info(
                        f"Storing pending task: {payload.next_task.type.value}"
                    )
                    self._pending_next_task = payload.next_task
                    execute_next_task_immediately = False  # Don't execute now

                # Request cancellation
                cancel_future = self._goal_handle.cancel_goal_async()
                cancel_future.add_done_callback(self.cancel_response_callback)
            else:
                self.get_logger().info(
                    "\033[93m[BrainClient] Stop received but no goal handle active.\033[0m"
                )

        if payload.thoughts:
            chat_entry = MessageOut(
                type=MessageOutType.CHAT_OUT,
                payload={"text": payload.thoughts},
            )
            self._handle_chat_out(chat_entry, sender="robot_thoughts")

        if payload.to_tell_user:
            chat_entry = MessageOut(
                type=MessageOutType.CHAT_OUT,
                payload={"text": payload.to_tell_user},
            )
            self._handle_chat_out(chat_entry, sender="robot")

        if payload.anticipation:
            chat_entry = MessageOut(
                type=MessageOutType.CHAT_OUT,
                payload={"text": payload.anticipation},
            )
            self._handle_chat_out(chat_entry, sender="robot_anticipation")

        # Only execute the next task immediately if not stopping/pending
        if execute_next_task_immediately and payload.next_task is not None:
            # Ensure any previously pending task is cleared if we are executing a new one directly
            self._pending_next_task = None

            self.get_logger().info(
                f"\033[92m[BrainClient] Next task: {payload.next_task}\033[0m"
            )
            self.get_logger().info(
                f"Primitive task type: {payload.next_task.type.value}"
            )

            if payload.next_task.type.value in self.primitives_dict:
                self.send_primitive_goal(
                    payload.next_task.type,
                    payload.next_task.inputs,
                )
                status_msg = MessageIn(
                    type=MessageInType.PRIMITIVE_ACTIVATED,
                    payload={
                        "primitive_name": payload.next_task.type.value,
                        "primitive_id": payload.next_task.primitive_id,
                    },
                )
                self.ws_bridge.send_message(status_msg)
                self.primitive_running = {
                    "primitive_name": payload.next_task.type.value,
                    "primitive_id": payload.next_task.primitive_id,
                }
            else:
                self.get_logger().warn(
                    f"Unknown primitive type: {payload.next_task.type}"
                )
        elif not execute_next_task_immediately and payload.next_task is not None:
            self.get_logger().info(
                "\033[94m[BrainClient] Next task stored, waiting for cancellation to complete.\033[0m"
            )
        else:
            # Ensure pending task is cleared if no next task is given
            self._pending_next_task = None
            self.get_logger().info(
                "\033[94m[BrainClient] No next task provided or task is pending.\033[0m"
            )

    def agent_loop_callback(self):
        # This callback will send the RGB image and, if allowed, the depth image
        if not self.primitives_registered:
            # If primitives are not yet registered, don't send images
            self.get_logger().info(
                "\033[93m[BrainClient] Primitives not registered. Skipping image callback.\033[0m"
            )
            return

        if self.ready_for_image and self.last_image is not None:
            self.get_logger().info(
                "\033[93m[BrainClient] Sending image callback.\033[0m"
            )
            try:
                # Compress the RGB image as JPEG (70% quality)
                encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 70]
                success, encoded_img = cv2.imencode(
                    ".jpg", self.last_image, encode_params
                )
                if not success:
                    self.get_logger().error("Failed to encode RGB image")
                    self.ready_for_image = False
                    return

                rgb_b64 = base64.b64encode(encoded_img.tobytes()).decode("utf-8")
                payload = {"image_b64": rgb_b64}

                # Include the horizontal and vertical FOV of the camera
                payload["horizontal_fov"] = self.horizontal_fov
                payload["vertical_fov"] = self.vertical_fov

                # Optionally include depth data if enabled and available.
                if self.send_depth and self.last_depth_image is None:
                    self.get_logger().warn(
                        "\033[93m[BrainClient] No depth image available.\033[0m"
                    )
                    return
                elif self.send_depth and self.last_depth_image is not None:
                    depth_frame = self.last_depth_image
                    depth_data = depth_frame.tobytes()
                    if depth_frame.dtype == np.uint16:
                        encoding = "16UC1"
                        bytes_per_pixel = 2
                    elif depth_frame.dtype == np.float32:
                        encoding = "32FC1"
                        bytes_per_pixel = 4
                    else:
                        encoding = "8UC1"
                        bytes_per_pixel = 1
                    depth_payload = {
                        "height": int(depth_frame.shape[0]),
                        "width": int(depth_frame.shape[1]),
                        "encoding": encoding,
                        "is_bigendian": 0,
                        "step": int(depth_frame.shape[1] * bytes_per_pixel),
                        "data": base64.b64encode(depth_data).decode("utf-8"),
                    }
                    payload["depth"] = depth_payload

                # Include map data if available
                if self.last_map is not None:
                    # Create a simplified map payload with only the necessary information
                    map_data = self.last_map.data

                    # Convert quaternion to yaw
                    ori = self.last_map.info.origin.orientation
                    siny_cosp = 2.0 * (ori.w * ori.z + ori.x * ori.y)
                    cosy_cosp = 1.0 - 2.0 * (ori.y * ori.y + ori.z * ori.z)
                    yaw = math.atan2(siny_cosp, cosy_cosp)

                    map_payload = {
                        "resolution": self.last_map.info.resolution,
                        "width": self.last_map.info.width,
                        "height": self.last_map.info.height,
                        "origin_x": self.last_map.info.origin.position.x,
                        "origin_y": self.last_map.info.origin.position.y,
                        "origin_z": self.last_map.info.origin.position.z,
                        "origin_yaw": yaw,
                        "frame_id": self.last_map.header.frame_id,
                        "data": base64.b64encode(np.array(map_data).tobytes()).decode(
                            "utf-8"
                        ),
                    }
                    payload["map"] = map_payload
                    self.get_logger().debug("Including map data in image message")

                else:
                    self.get_logger().warn(
                        "\033[93m[BrainClient] No map data available.\033[0m"
                    )
                    return

                # Include robot coordinates (if available) in the payload.
                if self.last_odom is not None:
                    pos = self.last_odom.pose.pose.position
                    ori = self.last_odom.pose.pose.orientation
                    # Compute yaw from quaternion:
                    siny_cosp = 2.0 * (ori.w * ori.z + ori.x * ori.y)
                    cosy_cosp = 1.0 - 2.0 * (ori.y * ori.y + ori.z * ori.z)
                    theta = math.atan2(siny_cosp, cosy_cosp)
                    payload["robot_coords"] = {
                        "x": pos.x,
                        "y": pos.y,
                        "z": pos.z,
                        "theta": theta,
                        "frame_id": self.last_odom.header.frame_id,
                    }
                else:
                    self.get_logger().warn(
                        "\033[93m[BrainClient] No odometry data available.\033[0m"
                    )
                    return

                # Build and send the message
                image_msg = MessageIn(type=MessageInType.IMAGE, payload=payload)
                self.ws_bridge.send_message(image_msg)

                # Reset flags so we do not resend the same images
                self.ready_for_image = False
                self.last_depth_image = None
            except Exception as e:
                self.get_logger().error(f"Error in agent_loop_callback: {e}")
                raise

    def send_primitive_goal(self, task_type, inputs):
        goal_msg = ExecutePrimitive.Goal()
        goal_msg.primitive_type = task_type.value

        # Inputs are now only the direct arguments for the primitive's execute method.
        # Robot state injection is handled by the PrimitiveExecutionActionServer.
        goal_msg.inputs = json.dumps(inputs if inputs is not None else {})

        self.get_logger().info(
            f"Sending goal for primitive: {goal_msg.primitive_type} with inputs: {goal_msg.inputs}"
        )
        if not self.primitive_action_client.wait_for_server(timeout_sec=1.0):
            self.get_logger().error("Primitive execution action server not available!")
            return

        send_goal_future = self.primitive_action_client.send_goal_async(goal_msg)
        send_goal_future.add_done_callback(self.goal_response_callback)

    def goal_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.get_logger().info("Primitive execution goal rejected.")
            return
        # Store the goal handle for potential cancellation, using the same naming as in the example
        self._goal_handle = goal_handle
        self.get_logger().info("Primitive execution goal accepted.")
        get_result_future = goal_handle.get_result_async()
        get_result_future.add_done_callback(self.get_result_callback)

    def cancel_response_callback(self, future):
        """Handle the response from a cancel_goal_async request."""
        # This is not REALLY necessary, but for logging purposes it's nice to have.
        cancel_response = future.result()

        self.get_logger().info("\033[92m[BrainClient] Cancel response received.\033[0m")

        # Check if any goals were canceled, following the ROS2 example
        if (
            hasattr(cancel_response, "goals_canceling")
            and len(cancel_response.goals_canceling) > 0
        ):
            self.get_logger().info("Goal cancellation accepted.")
        else:
            self.get_logger().error("Goal cancellation rejected.")

    def get_result_callback(self, future):
        self.get_logger().info(" [92m[BrainClient] Get result callback. [0m")
        result = future.result().result

        status_color = "\033[92m" if result.success else "\033[91m"
        self.get_logger().info(
            f"{status_color}Primitive execution result: {result.success}, Type: {result.success_type}\033[0m"
        )

        # Clear the goal handle since the goal is complete
        self._goal_handle = None

        # Stop the robot (consider moving this if primitives should stop themselves)
        # Note that I think cancelling the goal will also send a stop command, so maybe we can look into that.
        stop_cmd = Twist()
        stop_cmd.linear.x = 0.0
        stop_cmd.angular.z = 0.0
        self.cmd_vel_pub.publish(stop_cmd)

        # --- Send status message FIRST ---
        # Get primitive info from the primitive_running dictionary
        primitive_id = None
        primitive_name = result.primitive_type  # Use name from result
        if (
            self.primitive_running
            and self.primitive_running["primitive_name"] == primitive_name
        ):
            primitive_id = self.primitive_running["primitive_id"]
        elif self.primitive_running:
            self.get_logger().warn(
                f"Primitive name mismatch in result ({primitive_name}) and running ({self.primitive_running['primitive_name']})"
            )
            primitive_id = self.primitive_running[
                "primitive_id"
            ]  # Use stored ID anyway?

        self.primitive_running = None  # Clear running state

        # Determine the appropriate message type based on the result
        self.get_logger().info(f"Primitive result details: {result}")
        outgoing_msg = None
        if result.success and result.success_type == PrimitiveResult.SUCCESS.value:
            outgoing_msg = MessageIn(
                type=MessageInType.PRIMITIVE_COMPLETED,
                payload={
                    "primitive_name": primitive_name,
                    "primitive_id": primitive_id,
                },
            )
        elif result.success_type == PrimitiveResult.CANCELLED.value:
            outgoing_msg = MessageIn(
                type=MessageInType.PRIMITIVE_INTERRUPTED,
                payload={
                    "primitive_name": primitive_name,
                    "primitive_id": primitive_id,
                },
            )
        elif not result.success or result.success_type == PrimitiveResult.FAILURE.value:
            outgoing_msg = MessageIn(
                type=MessageInType.PRIMITIVE_FAILED,
                payload={
                    "primitive_name": primitive_name,
                    "reason": result.message,
                    "primitive_id": primitive_id,
                },
            )
        else:
            # Log success status as well for debugging unknown cases
            self.get_logger().error(
                f"Unknown primitive result combination: success={result.success}, type={result.success_type}"
            )

        # Send the determined status message
        if outgoing_msg:
            self.ws_bridge.send_message(outgoing_msg)
            self.get_logger().info(
                f"Sent primitive status message: {outgoing_msg.type.name}"
            )

        # --- THEN, check and execute pending task if previous was cancelled ---
        if (
            result.success_type == PrimitiveResult.CANCELLED.value
            and self._pending_next_task is not None
        ):
            self.get_logger().info(
                f"Executing pending task after internal cancellation: {self._pending_next_task.type.value}"
            )
            pending_task = self._pending_next_task
            self._pending_next_task = None  # Clear before sending new goal
            status_msg = MessageIn(
                type=MessageInType.PRIMITIVE_ACTIVATED,
                payload={
                    "primitive_name": pending_task.type.value,
                    "primitive_id": pending_task.primitive_id,
                },
            )
            self.ws_bridge.send_message(status_msg)
            self.primitive_running = {
                "primitive_name": pending_task.type.value,
                "primitive_id": pending_task.primitive_id,
            }
            self.send_primitive_goal(
                pending_task.type, pending_task.inputs
            )
        elif self._pending_next_task is not None:
            # Clear pending task if the goal finished differently (SUCCESS/FAILURE)
            self.get_logger().warn(
                f"Clearing pending task {self._pending_next_task.type.value} because previous task finished with type {result.success_type}"
            )
            self._pending_next_task = None

    def _handle_primitives_and_directive_registered(self, msg):
        """
        Handle the PRIMITIVES_REGISTERED response from the server.
        """
        self.get_logger().info(f"Registration response: {msg.payload}")
        if msg.payload.get("success", False):
            self.primitives_registered = True
            self.directive_registered = True
            primitive_count = msg.payload.get("count", 0)
            directive_registered = msg.payload.get("directive_registered", False)
            self.get_logger().info(
                f"Successfully registered {primitive_count} primitives and directive: {directive_registered}"
            )

            # Start the pose image timer if we've already received a ready_for_image message
            if self.ready_for_image and not self.pose_image_started:
                self.get_logger().info(
                    "Starting regular pose image transmission after registration"
                )
                self.pose_image_started = True
                self.pose_image_timer = self.create_timer(
                    self.pose_image_interval, self.pose_image_callback
                )
        else:
            self.get_logger().error(
                "Failed to register primitives and/or directive with server"
            )

    def register_primitives_and_directive(self):
        """
        Collects information about available primitives and directive and sends it to the server
        for registration.
        """
        self.get_logger().info(
            "Collecting primitive and directive definitions for registration..."
        )

        # Prepare the registration data for primitives
        primitives = []
        if self.primitives_dict:
            for primitive_name, primitive in self.primitives_dict.items():
                # Extract parameter information using introspection
                params = {}
                signature = inspect.signature(primitive.execute)

                for param_name, param in signature.parameters.items():
                    if param_name == "self":
                        continue

                    # Get parameter type from annotation if available
                    param_type = "any"
                    if param.annotation != inspect.Parameter.empty:
                        # Handle UnionType (e.g., int | str) and GenericAlias (e.g., list[int])
                        if isinstance(param.annotation, (types.UnionType, types.GenericAlias)) or \
                           hasattr(param.annotation, '_name') and param.annotation._name in ['List', 'Optional', 'Dict', 'Tuple', 'Union']: # Covers typing.List, typing.Optional etc.
                            param_type = str(param.annotation)
                        elif hasattr(param.annotation, '__name__'):
                            param_type = param.annotation.__name__
                        else:
                            # Fallback for other complex types, str() might be a reasonable default
                            param_type = str(param.annotation)
                        # Clean up "typing." prefix if present
                        param_type = param_type.replace("typing.", "")

                    params[param_name] = f"{param_type}"

                primitives.append(
                    {
                        "name": primitive_name,
                        "guideline": primitive.guidelines(),
                        "inputs": params,
                    }
                )

        included_primitives = [
            p
            for p in primitives
            if p["name"] in self.current_directive.get_primitives()
        ]

        # Create and send the registration message
        reg_msg = MessageIn(
            type=MessageInType.REGISTER_PRIMITIVES_AND_DIRECTIVE,
            payload={
                "primitives": included_primitives if included_primitives else None,
                "directive": self.current_directive.get_prompt(),
                "token": self.token,
            },
        )
        self.get_logger().info(
            f"Registering {len(primitives)} primitives and directive '{self.current_directive.name}' with server"
        )
        self.ws_bridge.send_message(reg_msg)

    def set_directive_callback(self, msg: String):
        """
        Callback for changing the AI's directive.
        """
        directive_name = msg.data.strip()
        self.get_logger().info(f"Received directive change request: {directive_name}")

        if directive_name in self.directives:
            self.current_directive = self.directives[directive_name]
            self.get_logger().info(f"Activated directive: {directive_name}")

            # Re-register primitives and directive with the server to update
            self.register_primitives_and_directive()

            # Publish confirmation
            chat_entry = {
                "sender": "system",
                "text": f"Directive updated to: {directive_name}",
                "timestamp": time.time(),
            }
            self.chat_history.append(chat_entry)
            out_msg = String(data=json.dumps(chat_entry))
            self.chat_out_pub.publish(out_msg)
        else:
            self.get_logger().error(f"Unknown directive: {directive_name}")
            available_directives = list(self.directives.keys())
            error_msg = {
                "sender": "system",
                "text": f"Error: Unknown directive '{directive_name}'. Available directives: {available_directives}",
                "timestamp": time.time(),
            }
            self.chat_history.append(error_msg)
            out_msg = String(data=json.dumps(error_msg))
            self.chat_out_pub.publish(out_msg)

    def handle_get_available_directives(self, request, response):
        """
        Service handler that returns a list of all available directives.
        """
        self.get_logger().info("Received request for available directives")
        directive_names = list(self.directives.keys())
        response.directives = directive_names
        response.current_directive = self.current_directive.name
        return response

    def handle_reset_brain(self, request, response):
        """
        Service handler for resetting the brain.
        Sends a chat message to load the memory instead of a reset message.
        """
        self.get_logger().info("\033[1;92m[BrainClient] Resetting brain\033[0m")
        memory_state = request.memory_state
        self.get_logger().info(
            f"\033[1;92m[BrainClient] Memory state: {memory_state}\033[0m"
        )

        # As long as we don't have
        # confirmation that the new primitives have been registered, we should not
        # accept new VisionAgentOutput messages.
        self.primitives_registered = False

        # Clear local chat history
        self.chat_history = []

        # Stop any running primitive
        if self.primitive_running:
            self.get_logger().info(
                "\033[1;92m[BrainClient] Stopping running primitive\033[0m"
            )
            self.primitive_running = None
            # Send a stop command to the robot
            if self._goal_handle:
                self._goal_handle.cancel_goal_async()

            stop_cmd = Twist()
            stop_cmd.linear.x = 0.0
            stop_cmd.angular.z = 0.0
            self.cmd_vel_pub.publish(stop_cmd)

        # Send a reset message to the robot
        reset_msg = MessageIn(
            type=MessageInType.RESET, payload={"memory_state": memory_state}
        )
        self.ws_bridge.send_message(reset_msg)

        # Publish a system message to the chat
        chat_entry = {
            "sender": "system",
            "text": f"Brain has been reset with memory state: {memory_state}",
            "timestamp": time.time(),
        }
        self.chat_history.append(chat_entry)
        out_msg = String(data=json.dumps(chat_entry))
        self.chat_out_pub.publish(out_msg)

        # Re-register primitives and directive with the server
        self.register_primitives_and_directive()

        response.success = True
        return response

    def destroy_node(self):
        self.exit_event.set()
        # Cancel the pose image timer if it exists
        if self.pose_image_timer:
            self.pose_image_timer.cancel()
        return super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = BrainClientNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("KeyboardInterrupt, shutting down.")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
