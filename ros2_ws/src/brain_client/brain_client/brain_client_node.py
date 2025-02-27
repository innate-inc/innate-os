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
from sensor_msgs.msg import CompressedImage
from geometry_msgs.msg import Twist
from std_msgs.msg import String
from sensor_msgs.msg import Image
from nav_msgs.msg import Odometry
from brain_messages.srv import GetChatHistory
from brain_messages.action import ExecutePrimitive
from brain_client.primitives.navigate_to_position import NavigateToPosition
from brain_client.ws_bridge import WSBridge


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

        self.get_logger().info(f"Starting BrainClientNode with ws_uri={self.ws_uri}")

        # Publishers, Subscribers, and Service
        self.last_image = None
        self.last_depth_image = None

        # RGB image subscription remains unchanged.
        self.image_sub = self.create_subscription(
            CompressedImage, self.image_topic, self.image_callback, 10
        )

        # Optionally subscribe to the depth image topic if required.
        if self.send_depth:
            # Assuming that the depth image is published as a sensor_msgs/Image
            # (which contains: header, height, width, encoding, is_bigendian, step, data)

            self.depth_image_sub = self.create_subscription(
                Image, self.depth_image_topic, self.depth_image_callback, 10
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

        self.exit_event = threading.Event()
        self.ready_for_image = False

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
            # Add other primitives here as they become available
        }

        self.primitive_running = False
        self.directive = "You have a sassy personality and are a bit of a jerk when you talk to people."

        # Add a subscription to change directive
        self.directive_sub = self.create_subscription(
            String, "/set_directive", self.directive_callback, 10
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
        self.get_logger().debug(f"Received chat_in: {chat_entry}")
        outgoing_msg = MessageIn(type=MessageInType.CHAT_IN, payload={"text": msg.data})
        self.ws_bridge.send_message(outgoing_msg)

    def handle_get_chat_history(self, request, response):
        self.get_logger().debug(
            f"Received get_chat_history request. History: {self.chat_history}"
        )
        response.history = json.dumps(self.chat_history)
        return response

    def image_callback(self, msg: CompressedImage):
        try:
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

    def _handle_vision_agent_output(self, msg):
        try:
            self.get_logger().debug(f"[BrainClient] VisionAgentOutput: {msg}")
            # HOTFIX: If there's a msg.payload["next_task"] with a "name" field, replace it with "type".
            if "next_task" in msg.payload and msg.payload["next_task"] is not None:
                if "name" in msg.payload["next_task"]:
                    msg.payload["next_task"]["type"] = msg.payload["next_task"]["name"]
                    msg.payload["next_task"].pop("name", None)
            payload = VisionAgentOutput.model_validate(msg.payload)
            self.handle_vision_agent_output(payload)
        except Exception as e:
            self.get_logger().error(
                f"Error processing vision output: {e}. Traceback: {traceback.format_exc()}"
            )

    def _handle_chat_out(self, msg, sender="robot"):
        text = msg.payload.get("text", "")
        chat_entry = {"sender": sender, "text": text, "timestamp": time.time()}
        self.chat_history.append(chat_entry)
        self.get_logger().info(f"Received chat_out: {chat_entry}")
        out_msg = String(data=json.dumps(chat_entry))
        self.chat_out_pub.publish(out_msg)

    def handle_vision_agent_output(self, payload: VisionAgentOutput):
        if payload.stop_current_task:
            self.get_logger().info("\033[91m[BrainClient] Stop signal received.\033[0m")
            self.primitive_running = False
            self.primitive_action_client.cancel_goal_async()
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

            if payload.next_task.type == TaskType.NAVIGATE_TO_POSITION:
                self.send_primitive_goal(
                    payload.next_task.type, payload.next_task.inputs
                )
                status_msg = MessageIn(
                    type=MessageInType.PRIMITIVE_ACTIVATED,
                    payload={"primitive_name": payload.next_task.type.value},
                )
                self.ws_bridge.send_message(status_msg)
                self.primitive_running = True
            else:
                self.get_logger().warn(
                    "\033[93m[BrainClient] No valid task provided.\033[0m"
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
                if self.send_depth and self.last_depth_image is not None:
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
                self.get_logger().info(
                    "Published image message with optional depth and robot coordinates."
                )

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

        self.get_logger().info(
            f"\033[96m[BrainClient] Sending goal for primitive: {goal_msg.primitive_type}\033[0m"
        )
        if not self.primitive_action_client.wait_for_server(timeout_sec=1.0):
            self.get_logger().error(
                "\033[91m[BrainClient] Primitive execution action server not available!\033[0m"
            )
            return

        send_goal_future = self.primitive_action_client.send_goal_async(goal_msg)
        send_goal_future.add_done_callback(self.goal_response_callback)

    def goal_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.get_logger().info(
                "\033[91m[BrainClient] Primitive execution goal rejected.\033[0m"
            )
            return
        self.get_logger().info(
            "\033[92m[BrainClient] Primitive execution goal accepted.\033[0m"
        )
        get_result_future = goal_handle.get_result_async()
        get_result_future.add_done_callback(self.get_result_callback)

    def get_result_callback(self, future):
        result = future.result().result
        status_color = "\033[92m" if result.success else "\033[91m"
        self.get_logger().info(
            f"{status_color}[BrainClient] Primitive execution result: {result.success}\033[0m"
        )
        if result.success:
            self.primitive_running = False
            outgoing_msg = MessageIn(
                type=MessageInType.PRIMITIVE_COMPLETED,
                payload={"primitive_name": result.primitive_type},
            )
            self.ws_bridge.send_message(outgoing_msg)

    def _handle_primitives_and_directive_registered(self, msg):
        """
        Handle the PRIMITIVES_REGISTERED response from the server.
        """
        self.get_logger().info(f"Registration response: {msg.payload}")
        if msg.payload.get("success", False):
            self.primitives_registered = True
            self.directive_registered = True
            primitive_count = msg.payload.get("primitive_count", 0)
            directive_registered = msg.payload.get("directive_registered", False)
            self.get_logger().info(
                f"Successfully registered {primitive_count} primitives and directive: {directive_registered}"
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

        directive = self.directive

        # Create and send the registration message
        reg_msg = MessageIn(
            type=MessageInType.REGISTER_PRIMITIVES_AND_DIRECTIVE,
            payload={
                "primitives": primitives if primitives else None,
                "directive": directive,  # Single directive, can be None
                "token": self.token,  # Include authentication token
            },
        )
        self.get_logger().info(
            f"Registering {len(primitives)} primitives and directive with server"
        )
        self.ws_bridge.send_message(reg_msg)

    def directive_callback(self, msg: String):
        """
        Callback for changing the AI's directive.
        """
        new_directive = msg.data
        self.get_logger().info(f"Received new directive: {new_directive}")
        self.directive = new_directive

        # Re-register primitives and directive with the server to update
        self.register_primitives_and_directive()

        # Publish confirmation
        chat_entry = {
            "sender": "system",
            "text": f"Directive updated to: {new_directive}",
            "timestamp": time.time(),
        }
        self.chat_history.append(chat_entry)
        out_msg = String(data=json.dumps(chat_entry))
        self.chat_out_pub.publish(out_msg)

    def destroy_node(self):
        self.exit_event.set()
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
