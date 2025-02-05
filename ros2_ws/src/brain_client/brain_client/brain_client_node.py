#!/usr/bin/env python3

import rclpy
from rclpy.node import Node

import asyncio
import threading
import json
import time

import cv2
import numpy as np
import websockets

from brain_client.message_types import (
    MessageIn,
    MessageInType,
    MessageOut,
    MessageOutType,
    TaskType,
    VisionAgentOutput,
)
from sensor_msgs.msg import CompressedImage
from geometry_msgs.msg import Twist, Vector3
from std_msgs.msg import String
from brain_messages.srv import GetChatHistory

from brain_client.ws_client import WSClient


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
        self.chat_out_pub = self.create_publisher(String, "/chat_out", 10)
        self.get_chat_history_srv = self.create_service(
            GetChatHistory, "/get_chat_history", self.handle_get_chat_history
        )

        # Exit event and image readiness flag
        self.exit_event = threading.Event()
        self.ready_for_image = False

        # Timer for checking image readiness
        self.agent_timer = self.create_timer(0.1, self.agent_loop_callback)

        # Set up WebSocket client and register message handlers
        self.ws_client = WSClient(self.ws_uri, self.token, self)
        self.ws_client.register_handler(
            MessageOutType.READY_FOR_IMAGE, self._handle_ready_for_image
        )
        self.ws_client.register_handler(
            MessageOutType.VISION_AGENT_OUTPUT, self._handle_vision_agent_output
        )
        self.ws_client.register_handler(MessageOutType.CHAT_OUT, self._handle_chat_out)

        self.ws_thread = threading.Thread(target=self.run_ws_loop, daemon=True)
        self.ws_thread.start()

    def chat_in_callback(self, msg: String):
        chat_entry = {"sender": "user", "text": msg.data, "timestamp": time.time()}
        self.chat_history.append(chat_entry)
        self.get_logger().info(f"Received chat_in: {chat_entry}")

        if self.ws_client and self.ws_client.websocket:
            chat_msg = MessageIn(type=MessageInType.CHAT_IN, payload={"text": msg.data})
            asyncio.run_coroutine_threadsafe(
                self.ws_client.send(chat_msg), self.ws_client.loop
            )

    def handle_get_chat_history(self, request, response):
        response.history = json.dumps(self.chat_history)
        return response

    def image_callback(self, msg: CompressedImage):
        try:
            np_arr = np.frombuffer(msg.data, np.uint8)
            self.last_image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        except Exception as e:
            self.get_logger().error(f"Failed to decode compressed image: {e}")

    async def _handle_ready_for_image(self, msg):
        self.get_logger().debug("Received READY_FOR_IMAGE; setting flag.")
        self.ready_for_image = True

    async def _handle_vision_agent_output(self, msg):
        try:
            payload = VisionAgentOutput.model_validate(msg.payload)
            self.handle_vision_agent_output(payload)
        except Exception as e:
            self.get_logger().error(f"Error processing vision output: {e}")

    async def _handle_chat_out(self, msg):
        text = msg.payload.get("text", "")
        chat_entry = {"sender": "cloud", "text": text, "timestamp": time.time()}
        self.chat_history.append(chat_entry)
        self.get_logger().info(f"Received chat_out: {chat_entry}")
        out_msg = String(data=text)
        self.chat_out_pub.publish(out_msg)

    def handle_vision_agent_output(self, payload: VisionAgentOutput):
        if payload.next_task is not None:
            self.get_logger().debug(f"[BrainClient] Next task: {payload.next_task}")
            if payload.next_task.type == TaskType.VELOCITY_CONTROL:
                velocity_dict = json.loads(payload.next_task.description)
                twist_msg = Twist(
                    linear=Vector3(x=velocity_dict["forward"]),
                    angular=Vector3(z=velocity_dict["angle"]),
                )
                self.cmd_vel_pub.publish(twist_msg)
            if payload.next_task.type == TaskType.NAVIGATION_TO_POSITION:
                self.get_logger().info(
                    f"[BrainClient] Navigating to position: {payload.next_task.description}"
                )
                # TODO: Implement navigation to position
        else:
            self.get_logger().info("[BrainClient] No next task")
        if payload.stop_current_task:
            self.get_logger().info("[BrainClient] Stopping current task")

    def agent_loop_callback(self):
        if self.ready_for_image and self.last_image is not None:
            self.get_logger().debug("Agent loop: sending image")
            asyncio.run_coroutine_threadsafe(self.send_image(), self.ws_client.loop)
            self.ready_for_image = False

    async def send_image(self):
        if self.last_image is not None:
            await self.ws_client.send_image(self.last_image)
            self.last_image = None

    def run_ws_loop(self):
        loop = asyncio.new_event_loop()
        self.ws_client.loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.ws_client.connect())
        loop.close()

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
