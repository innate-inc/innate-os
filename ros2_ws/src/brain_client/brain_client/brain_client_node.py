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

from brain_client.message_types import (
    InternalMessage,
    InternalMessageType,
    MessageIn,
    MessageInType,
    MessageOutType,
    TaskType,
    VisionAgentOutput,
)
from brain_client.primitives.navigate_to_position import NavigateToPosition
from sensor_msgs.msg import CompressedImage
from geometry_msgs.msg import Twist
from std_msgs.msg import String
from brain_messages.srv import GetChatHistory

# Import our WSBridge class.
from brain_client.ws_bridge import WSBridge


class BrainClientNode(Node):
    def __init__(self):
        super().__init__("brain_client_node")

        # Parameters
        self.declare_parameter("websocket_uri", "ws://localhost:8765")
        self.declare_parameter("token", "MY_HARDCODED_TOKEN")
        self.declare_parameter("image_topic", "/camera/color/image_raw/compressed")
        self.declare_parameter("cmd_vel_topic", "/cmd_vel")

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

        self.get_logger().info(f"Starting BrainClientNode with ws_uri={self.ws_uri}")

        # Publishers, subscribers, and service
        self.last_image = None
        self.image_sub = self.create_subscription(
            CompressedImage, self.image_topic, self.image_callback, 10
        )
        self.cmd_vel_pub = self.create_publisher(Twist, self.cmd_vel_topic, 10)

        self.chat_history = []
        self.chat_in_sub = self.create_subscription(
            String, "/chat_in", self.chat_in_callback, 10
        )
        # This publisher is still available if needed locally.
        self.chat_out_pub = self.create_publisher(String, "/chat_out", 10)
        self.get_chat_history_srv = self.create_service(
            GetChatHistory, "/get_chat_history", self.handle_get_chat_history
        )

        # Primitives defined here
        self.primitives_available = {
            TaskType.NAVIGATE_TO_POSITION: NavigateToPosition(self.get_logger()),
        }

        # Exit event and image readiness flag
        self.exit_event = threading.Event()
        self.ready_for_image = False

        # Timer for checking image readiness
        self.agent_timer = self.create_timer(0.1, self.agent_loop_callback)

        # Instantiate the WSBridge.
        # It subscribes to "ws_messages" (incoming) and publishes on "ws_outgoing" (outgoing).
        self.ws_bridge = WSBridge(
            self, incoming_topic="ws_messages", outgoing_topic="ws_outgoing"
        )
        # Register handlers with WSBridge.
        self.ws_bridge.register_handler(
            MessageOutType.READY_FOR_IMAGE, self._handle_ready_for_image
        )
        self.ws_bridge.register_handler(
            MessageOutType.VISION_AGENT_OUTPUT, self._handle_vision_agent_output
        )
        self.ws_bridge.register_handler(MessageOutType.CHAT_OUT, self._handle_chat_out)

        # Tell the ws_bridge we're ready to connect
        for _ in range(3):
            self.ws_bridge.send_message(
                InternalMessage(type=InternalMessageType.READY_FOR_CONNECTION)
            )
            time.sleep(1.0)

        # Boolean flag to check if a primitive is running
        self.primitive_running = False

        # Create and start an asyncio event loop in a background thread
        self.async_loop = asyncio.new_event_loop()
        thread = threading.Thread(target=self.async_loop.run_forever, daemon=True)
        thread.start()

    def chat_in_callback(self, msg: String):
        chat_entry = {"sender": "user", "text": msg.data, "timestamp": time.time()}
        self.chat_history.append(chat_entry)
        self.get_logger().info(f"Received chat_in: {chat_entry}")
        # Send outgoing chat message via WSBridge.
        outgoing_msg = MessageIn(type=MessageInType.CHAT_IN, payload={"text": msg.data})
        self.ws_bridge.send_message(outgoing_msg)

    def handle_get_chat_history(self, request, response):
        self.get_logger().info(
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

    def _handle_ready_for_image(self, msg):
        self.get_logger().info("Received READY_FOR_IMAGE; setting flag.")
        self.ready_for_image = True

    def _handle_vision_agent_output(self, msg):
        try:
            self.get_logger().info(f"[BrainClient] VisionAgentOutput: {msg}")
            payload = VisionAgentOutput.model_validate(msg.payload)
            self.handle_vision_agent_output(payload)
        except Exception as e:
            self.get_logger().error(
                f"Error processing vision output: {e}. Traceback: {traceback.format_exc()}"
            )

    def _handle_chat_out(self, msg):
        text = msg.payload.get("text", "")
        chat_entry = {"sender": "cloud", "text": text, "timestamp": time.time()}
        self.chat_history.append(chat_entry)
        self.get_logger().info(f"Received chat_out: {chat_entry}")
        out_msg = String(data=text)
        self.chat_out_pub.publish(out_msg)

    def handle_vision_agent_output(self, payload: VisionAgentOutput):
        if payload.next_task is not None:
            self.get_logger().info(f"[BrainClient] Next task: {payload.next_task}")

            if payload.next_task.type == TaskType.NAVIGATE_TO_POSITION:
                # Get the instantiated primitive
                primitive = self.primitives_available[payload.next_task.type]

                # Send a message to the brain to indicate that the primitive is activated
                status_msg = MessageIn(
                    type=MessageInType.PRIMITIVE_ACTIVATED,
                    payload={"primitive_name": payload.next_task.type.value},
                )
                self.ws_bridge.send_message(status_msg)

                # Execute the primitive
                primitive.execute(**payload.next_task.inputs)
            else:
                self.get_logger().info("[BrainClient] No valid task provided.")
        else:
            self.get_logger().info("[BrainClient] No next task provided.")

    def agent_loop_callback(self):
        if self.ready_for_image and self.last_image is not None:
            self.get_logger().debug("Agent loop: sending image")

            success, encoded_img = cv2.imencode(".jpg", self.last_image)
            if success:
                b64_img = base64.b64encode(encoded_img.tobytes()).decode("utf-8")
                image_payload = {"image_b64": b64_img}
                image_msg = MessageIn(type=MessageInType.IMAGE, payload=image_payload)
                self.ws_bridge.send_message(image_msg)
                self.get_logger().debug("Published image message.")
            self.ready_for_image = False

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
