#!/usr/bin/env python3

import rclpy
from rclpy.node import Node

import asyncio
import threading
import json
import base64

import cv2
import numpy as np
import websockets

from brain_client.message_types import MessageType, TaskType, VisionAgentOutput
from sensor_msgs.msg import Image
from geometry_msgs.msg import Twist, Vector3

# If using CV Bridge to convert sensor_msgs/Image -> OpenCV
from cv_bridge import CvBridge


class BrainClientNode(Node):
    def __init__(self):
        super().__init__("brain_client_node")

        # Declare parameters
        self.declare_parameter("websocket_uri", "ws://localhost:8765")
        self.declare_parameter("token", "MY_HARDCODED_TOKEN")
        self.declare_parameter("image_topic", "/camera/color/image_raw")
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

        # Subscribe to the image topic
        self.bridge = CvBridge()
        self.last_image = None
        self.image_sub = self.create_subscription(
            Image, self.image_topic, self.image_callback, 10
        )

        # Publisher for cmd_vel
        self.cmd_vel_pub = self.create_publisher(Twist, self.cmd_vel_topic, 10)

        # We'll run the asyncio loop in a separate thread.
        self.exit_event = threading.Event()
        self.ws_thread = threading.Thread(target=self.run_async_loop, daemon=True)
        self.ws_thread.start()

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

    def image_callback(self, msg: Image):
        """
        Store the latest image in memory. We only send it to the cloud
        when the server says "ready_for_image".
        """
        # Convert ROS Image -> OpenCV
        try:
            cv_image = self.bridge.imgmsg_to_cv2(msg, desired_encoding="bgr8")
            self.last_image = cv_image
        except Exception as e:
            self.get_logger().error(f"Failed to convert image: {e}")

    def run_async_loop(self):
        """
        Start an asyncio event loop that connects to the cloud server
        and handles the messaging protocol.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.agent_loop_ws(self.ws_uri, self.token))
        loop.close()

    async def agent_loop_ws(self, server_uri: str, token: str):
        """
        Main coroutine that:
          - Connects to the cloud WebSocket server
          - Sends an auth token
          - Waits for commands from the server (e.g. "ready_for_image")
        """
        self.get_logger().debug(f"[BrainClient] Connecting to {server_uri} ...")

        try:
            async with websockets.connect(server_uri) as websocket:
                self.get_logger().info(f"[BrainClient] Connected to {server_uri}")

                # 1) Send authentication token immediately
                await websocket.send(token)
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

                    msg_type = data.get("type", "")

                    if msg_type == "ready_for_image":
                        self.get_logger().debug(
                            "[BrainClient] Received 'ready_for_image'"
                        )
                        # Send the latest image
                        should_send_image = True

                    elif msg_type == "well_received":
                        self.get_logger().debug(
                            "[BrainClient] Received 'well_received'"
                        )

                    elif msg_type == MessageType.VISION_AGENT_OUTPUT.value:
                        # Parse the payload as a VisionAgentOutput model
                        try:
                            # First parse the payload string into a dictionary
                            payload_dict = json.loads(data.get("payload", "{}"))
                            payload = VisionAgentOutput.model_validate(payload_dict)
                            self.get_logger().debug(
                                f"[BrainClient] vision_agent_output: {payload}"
                            )

                            self.handle_vision_agent_output(payload)
                        except Exception as e:
                            self.get_logger().error(
                                f"Failed to parse vision_agent_output: {e}"
                            )

                    else:
                        self.get_logger().debug(
                            f"[BrainClient] Unknown message type: {msg_type}"
                        )

                    # short sleep
                    await asyncio.sleep(0.01)

        except Exception as e:
            self.get_logger().error(f"[BrainClient] WebSocket connection error: {e}")

        self.get_logger().info("[BrainClient] Stopped agent_loop_ws.")

    async def send_image_over_ws(self, websocket, cv_image):
        """
        Encodes the OpenCV frame as JPEG, base64-encodes it,
        and sends it via WebSockets as a JSON message.
        """
        success, encoded_img = cv2.imencode(".jpg", cv_image)
        if not success:
            self.get_logger().error("Failed to encode image as JPEG.")
            return

        b64_img = base64.b64encode(encoded_img.tobytes()).decode("utf-8")

        msg = {"type": "image", "image_b64": b64_img}
        await websocket.send(json.dumps(msg))
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
