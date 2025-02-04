#!/usr/bin/env python3

import rclpy
from rclpy.node import Node

import asyncio
import threading
import json
import base64
import time  # For chat message timestamps

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


class BrainClientNode(Node):
    def __init__(self):
        super().__init__("brain_client_node")

        # Declare parameters
        self.declare_parameter("websocket_uri", "ws://localhost:8765")
        self.declare_parameter("token", "MY_HARDCODED_TOKEN")
        self.declare_parameter("image_topic", "/camera/color/image_raw/compressed")
        self.declare_parameter("cmd_vel_topic", "/cmd_vel")

        # Read parameter values
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

        # Subscribe to the compressed image topic
        self.last_image = None
        self.image_sub = self.create_subscription(
            CompressedImage, self.image_topic, self.image_callback, 10
        )

        # Publisher for cmd_vel
        self.cmd_vel_pub = self.create_publisher(Twist, self.cmd_vel_topic, 10)

        self.chat_history = []  # Locally stored chat history

        # Subscribe to /chat_in from ROS local side
        self.chat_in_sub = self.create_subscription(
            String, "/chat_in", self.chat_in_callback, 10
        )

        # Publisher for /chat_out
        self.chat_out_pub = self.create_publisher(String, "/chat_out", 10)

        # Create a service for retrieving chat history
        self.get_chat_history_srv = self.create_service(
            GetChatHistory, "/get_chat_history", self.handle_get_chat_history
        )
        #
        # [END ADDED chat]

        # We'll run the asyncio loop in a separate thread.
        self.exit_event = threading.Event()
        self.ws_thread = threading.Thread(target=self.run_async_loop, daemon=True)
        self.ws_thread.start()

    def chat_in_callback(self, msg: String):
        """
        Called whenever a local /chat_in message is received.
        We'll append it to local chat history (sender = "user")
        and bounce it back on /chat_out as a confirmation.
        """
        chat_entry = {
            "sender": "user",
            "text": msg.data,
            "timestamp": time.time(),
        }
        self.chat_history.append(chat_entry)
        self.get_logger().info(f"Received chat_in: {chat_entry}")

        # To fill: Send the chat_in message to the cloud
        if self.websocket is not None:
            chat_msg = MessageIn(
                type=MessageInType.CHAT_IN,
                payload={"text": msg.data},
            )
            asyncio.run_coroutine_threadsafe(
                self.websocket.send(chat_msg.model_dump_json()), self.loop
            )

    def handle_get_chat_history(self, request, response):
        """
        Service callback that returns the entire chat history as a JSON string.
        This corresponds to myrobot_msgs/srv/GetChatHistory.
        Adjust as needed to match your actual service definition.
        """
        response.history = json.dumps(self.chat_history)
        return response

    #
    # [END ADDED chat callbacks]
    #

    def handle_vision_agent_output(self, payload: VisionAgentOutput):
        """
        Handle the vision agent output.
        """
        if payload.next_task is not None:
            self.get_logger().debug(f"[BrainClient] Next task: {payload.next_task}")
            if payload.next_task.type == TaskType.VELOCITY_CONTROL:
                # Comes in like {"forward": 1.0, "angle": 2.0 if self.turn_right else -2.0}
                self.get_logger().debug("[BrainClient] Velocity control task")
                velocity_dict = json.loads(payload.next_task.description)
                self.cmd_vel_pub.publish(
                    Twist(
                        linear=Vector3(x=velocity_dict["forward"]),
                        angular=Vector3(z=velocity_dict["angle"]),
                    )
                )
        if payload.stop_current_task:
            self.get_logger().info("[BrainClient] Stopping current task")

    def image_callback(self, msg: CompressedImage):
        """
        Store the latest compressed image in memory. We only send it to the cloud
        when the server says "ready_for_image".
        """
        try:
            # Convert compressed image data to OpenCV format
            np_arr = np.frombuffer(msg.data, np.uint8)
            cv_image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            self.last_image = cv_image
        except Exception as e:
            self.get_logger().error(f"Failed to decode compressed image: {e}")

    def run_async_loop(self):
        """
        Start an asyncio event loop that connects to the cloud server
        and handles the messaging protocol.
        """
        loop = asyncio.new_event_loop()
        self.loop = loop  # Store reference to the event loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.agent_loop_ws(self.ws_uri, self.token))
        loop.close()

    async def agent_loop_ws(self, server_uri: str, token: str):
        """
        Main coroutine that:
          - Connects to the cloud WebSocket server
          - Sends an auth token (now wrapped in MessageIn)
          - Waits for commands from the server (e.g. "ready_for_image")
        """
        self.get_logger().debug(f"[BrainClient] Connecting to {server_uri} ...")

        # We'll store a reference to the websocket if needed for forwarding chat
        self.websocket = None

        try:
            async with websockets.connect(server_uri) as websocket:
                self.websocket = websocket
                self.get_logger().info(f"[BrainClient] Connected to {server_uri}")

                auth_msg = MessageIn(type=MessageInType.AUTH, payload={"token": token})
                await websocket.send(auth_msg.model_dump_json())
                self.get_logger().debug("[BrainClient] Token sent")

                should_send_image = False

                # 2) Main loop
                while rclpy.ok() and not self.exit_event.is_set():
                    if should_send_image:
                        if self.last_image is not None:
                            await self.send_image_over_ws(websocket, self.last_image)
                            self.last_image = None
                            should_send_image = False

                    try:
                        incoming_msg = await asyncio.wait_for(
                            websocket.recv(), timeout=0.1
                        )
                    except asyncio.TimeoutError:
                        # No message, normal
                        await asyncio.sleep(0.01)
                        continue
                    except websockets.exceptions.ConnectionClosed:
                        self.get_logger().warn("Server closed the connection.")
                        break

                    # Parse inbound JSON
                    try:
                        data = json.loads(incoming_msg)
                    except json.JSONDecodeError:
                        self.get_logger().warn("Received non-JSON data. Ignoring.")
                        continue

                    msg = MessageOut.model_validate(data)
                    msg_type = msg.type

                    if msg_type == MessageOutType.READY_FOR_IMAGE:
                        self.get_logger().debug(
                            "[BrainClient] Received 'ready_for_image'"
                        )
                        should_send_image = True

                    elif msg_type == MessageOutType.VISION_AGENT_OUTPUT:
                        try:
                            payload = VisionAgentOutput.model_validate(msg.payload)
                            self.handle_vision_agent_output(payload)
                        except Exception as e:
                            self.get_logger().error(
                                f"Failed to parse vision_agent_output: {e}"
                            )

                    elif msg_type == MessageOutType.CHAT_OUT:
                        text = msg.payload.get("text", "")
                        chat_entry = {
                            "sender": "cloud",
                            "text": text,
                            "timestamp": time.time(),
                        }
                        self.chat_history.append(chat_entry)
                        self.get_logger().info(
                            f"Received chat_out from cloud: {chat_entry}"
                        )

                        # Publish to /chat_out locally
                        out_msg = String()
                        out_msg.data = text
                        self.chat_out_pub.publish(out_msg)

                    else:
                        self.get_logger().debug(
                            f"[BrainClient] Unknown message type: {msg_type}"
                        )

                    await asyncio.sleep(0.01)

        except Exception as e:
            self.get_logger().error(f"[BrainClient] WebSocket connection error: {e}")

        self.get_logger().info("[BrainClient] Stopped agent_loop_ws.")
        self.websocket = None

    async def send_image_over_ws(self, websocket, cv_image):
        """
        Encodes the OpenCV frame as JPEG, base64-encodes it, and sends it via WebSockets.
        Now the payload is wrapped in a MessageIn with type IMAGE.
        """
        success, encoded_img = cv2.imencode(".jpg", cv_image)
        if not success:
            self.get_logger().error("Failed to encode image as JPEG.")
            return

        b64_img = base64.b64encode(encoded_img.tobytes()).decode("utf-8")

        from brain_client.message_types import MessageIn, MessageInType

        msg_obj = MessageIn(
            type=MessageInType.IMAGE,
            payload={"image_b64": b64_img},
        )
        await websocket.send(msg_obj.model_dump_json())
        self.get_logger().debug("[BrainClient] Sent image to server.")

    def destroy_node(self):
        # Called when shutting down
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
