#!/usr/bin/env python3
import traceback
import rclpy
from rclpy.node import Node
import threading
import json
import time
import cv2
import base64
import numpy as np
import asyncio
from rclpy.action import ActionClient
import math
import inspect

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
from nav_msgs.msg import Odometry
from brain_messages.srv import GetChatHistory
from brain_messages.action import ExecutePrimitive
from brain_messages.srv import GetAvailableDirectives
from brain_messages.srv import ResetBrain
from std_srvs.srv import SetBool

from brain_client.ws_bridge import WSBridge

from brain_client.primitives.navigate_to_position import NavigateToPosition
from brain_client.primitives.send_email import SendEmail

from brain_client.directives.default_directive import DefaultDirective
from brain_client.directives.sassy_directive import SassyDirective
from brain_client.directives.friendly_guide_directive import FriendlyGuideDirective
from brain_client.directives.security_patrol_directive import SecurityPatrolDirective
from brain_client.directives.elder_safety_directive import ElderSafetyDirective


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

        # Set to True if you wish to receive and forward depth images as well
        self.declare_parameter("send_depth", True)
        self.declare_parameter("odom_topic", "/odom")

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
        self.send_depth = (
            self.get_parameter("send_depth").get_parameter_value().bool_value
        )
        self.odom_topic = (
            self.get_parameter("odom_topic").get_parameter_value().string_value
        )
        self.last_odom = None
        self.odom_sub = self.create_subscription(
            Odometry, self.odom_topic, self.odom_callback, 10
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

        # Publishers, Subscribers, and Service
        self.last_image = None
        self.last_depth_image = None

        # RGB image subscription remains unchanged.
        self.image_sub = self.create_subscription(
            CompressedImage, self.image_topic, self.image_callback, 1
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

        for _ in range(3):
            self.ws_bridge.send_message(
                InternalMessage(type=InternalMessageType.READY_FOR_CONNECTION)
            )
            time.sleep(1.0)

        # Initialize primitives dictionary
        self.primitives_dict = {
            TaskType.NAVIGATE_TO_POSITION.value: NavigateToPosition(self.get_logger()),
            TaskType.SEND_EMAIL.value: SendEmail(self.get_logger()),
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
                ElderSafetyDirective(),
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

        # After initializing the primitive_action_client
        # Register the primitives with the server
        self.register_primitives_and_directive()

        self.async_loop = asyncio.new_event_loop()
        thread = threading.Thread(target=self.async_loop.run_forever, daemon=True)
        thread.start()

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
            f"\033[1;92m[BrainClient] Set logging configuration: log_everything={self.log_everything}\033[0m"
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
                dtype = np.uint8
            depth_array = np.frombuffer(msg.data, dtype=dtype)
            depth_array = depth_array.reshape((msg.height, msg.width))
            self.last_depth_image = depth_array
        except Exception as e:
            self.get_logger().error(f"Failed to decode depth image: {e}")

    def odom_callback(self, msg: Odometry):
        self.last_odom = msg

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
            self.get_logger().debug(f"[BrainClient] VisionAgentOutput: {msg}")

            # HOTFIX: If there's a msg.payload["next_task"] with a "name" field, replace it with "type".
            if "next_task" in msg.payload and msg.payload["next_task"] is not None:
                if "name" in msg.payload["next_task"]:
                    msg.payload["next_task"]["type"] = msg.payload["next_task"]["name"]
                    msg.payload["next_task"].pop("name", None)

            payload = VisionAgentOutput.model_validate(msg.payload)

            # If log_everything is enabled, send the complete vision agent output as a chat message
            if self.log_everything:
                self.get_logger().info(
                    "\033[1;92m[BrainClient] Sending complete vision agent output\033[0m"
                )
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
        if payload.stop_current_task:
            self.get_logger().info("\033[91m[BrainClient] Stop signal received.\033[0m")

            # Cancel the current goal if it exists
            if self._goal_handle is not None:
                self.get_logger().info(
                    "\033[91m[BrainClient] Canceling current goal.\033[0m"
                )
                cancel_future = self._goal_handle.cancel_goal_async()
                cancel_future.add_done_callback(self.cancel_response_callback)
            return

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

        if payload.next_task is not None:
            self.get_logger().info(
                f"\033[92m[BrainClient] Next task: {payload.next_task}\033[0m"
            )

            self.get_logger().info(
                f"Primitive task type: {payload.next_task.type.value}"
            )

            if payload.next_task.type.value in self.primitives_dict:
                # Handle any task type that exists in the primitives dictionary
                self.send_primitive_goal(
                    payload.next_task.type, payload.next_task.inputs
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
        else:
            self.get_logger().info(
                "\033[94m[BrainClient] No next task provided.\033[0m"
            )

    def agent_loop_callback(self):
        # This callback will send the RGB image and, if allowed, the depth image
        if not self.primitives_registered:
            # If primitives are not yet registered, don't send images
            return

        if self.ready_for_image and self.last_image is not None:
            try:
                self.get_logger().info("Agent loop: sending image")

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
                    }

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
        goal_msg.inputs = json.dumps(inputs)

        self.get_logger().info(f"Sending goal for primitive: {goal_msg.primitive_type}")
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

        # Check if any goals were canceled, following the ROS2 example
        if (
            hasattr(cancel_response, "goals_canceling")
            and len(cancel_response.goals_canceling) > 0
        ):
            self.get_logger().debug("Goal cancellation accepted.")
        else:
            self.get_logger().error("Goal cancellation rejected.")

    def get_result_callback(self, future):
        result = future.result().result
        status_color = "\033[92m" if result.success else "\033[91m"
        self.get_logger().debug(f"Primitive execution result: {result.success}")

        # Clear the goal handle since the goal is complete
        self._goal_handle = None

        # Wait 1sec
        time.sleep(
            1
        )  # TODO: Find a better way to do this. And probably only if it was a navigation primitive?

        # Note that I think cancelling the goal will also send a stop command, so maybe we can look into that.
        stop_cmd = Twist()
        stop_cmd.linear.x = 0.0
        stop_cmd.angular.z = 0.0
        self.cmd_vel_pub.publish(stop_cmd)

        self.get_logger().info("Stopping robot")

        # Get primitive info from the primitive_running dictionary
        primitive_id = None
        if self.primitive_running:
            primitive_id = self.primitive_running["primitive_id"]

        # Check the success_type field to determine the appropriate message type
        self.get_logger().info(f"Primitive result: {result}")
        if result.success:
            if result.success_type == PrimitiveResult.SUCCESS.value:
                self.primitive_running = None
                outgoing_msg = MessageIn(
                    type=MessageInType.PRIMITIVE_COMPLETED,
                    payload={
                        "primitive_name": result.primitive_type,
                        "primitive_id": primitive_id,
                    },
                )
                self.ws_bridge.send_message(outgoing_msg)
            elif result.success_type == PrimitiveResult.CANCELLED.value:
                self.primitive_running = None
                outgoing_msg = MessageIn(
                    type=MessageInType.PRIMITIVE_INTERRUPTED,
                    payload={
                        "primitive_name": result.primitive_type,
                        "primitive_id": primitive_id,
                    },
                )
                self.ws_bridge.send_message(outgoing_msg)
        elif not result.success:
            self.primitive_running = None
            outgoing_msg = MessageIn(
                type=MessageInType.PRIMITIVE_FAILED,
                payload={
                    "primitive_name": result.primitive_type,
                    "reason": result.message,
                    "primitive_id": primitive_id,
                },
            )
            self.ws_bridge.send_message(outgoing_msg)
        else:
            self.get_logger().error(f"Unknown primitive result: {result}")

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
                        param_type = str(param.annotation.__name__)

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

        # Clear local chat history
        self.chat_history = []

        # Stop any running primitive
        if self.primitive_running:
            self.get_logger().info(
                "\033[1;92m[BrainClient] Stopping running primitive\033[0m"
            )
            self.primitive_running = None
            # Send a stop command to the robot
            # TODO: This WON'T work, Beeds to be implemented in the primitive action server.
            stop_cmd = Twist()
            stop_cmd.linear.x = 0.0
            stop_cmd.angular.z = 0.0
            self.cmd_vel_pub.publish(stop_cmd)

        # Send a chat message to load the memory instead of a reset message
        chat_msg = MessageIn(
            type=MessageInType.CHAT_IN, payload={"text": f"!load_memory {memory_state}"}
        )
        self.ws_bridge.send_message(chat_msg)

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
