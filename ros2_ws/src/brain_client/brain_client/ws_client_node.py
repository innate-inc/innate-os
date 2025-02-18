#!/usr/bin/env python3
import asyncio
import json
import threading
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

import websockets

# Import your message definitions
from brain_client.message_types import (
    InternalMessage,
    InternalMessageType,
    MessageIn,
    MessageOut,
    MessageInType,
    MessageOutType,
)


###############################################################################
# WSClient class – largely unchanged, but now it relies on its node to publish.
###############################################################################
class WSClient:
    def __init__(self, uri, token, node):
        """
        :param node: A reference to the ROS node, used for logging and publishing.
        """
        self.uri = uri
        self.token = token
        self.node = node
        self.websocket = None
        self.loop = None

    async def connect(self):
        self.node.get_logger().debug(f"[WSClient] Connecting to {self.uri} ...")
        try:
            async with websockets.connect(self.uri) as websocket:
                self.websocket = websocket
                self.node.get_logger().info(f"[WSClient] Connected to {self.uri}")

                # Send auth message upon connection.
                auth_msg = MessageIn(
                    type=MessageInType.AUTH,
                    payload={"token": self.token},
                )
                await self.send(auth_msg)
                self.node.get_logger().debug("[WSClient] Auth message sent.")

                await self.listen()
        except Exception as e:
            self.node.get_logger().error(f"[WSClient] Connection error: {e}")

        self.node.get_logger().info("[WSClient] Stopping WSClient.")
        self.websocket = None

    async def listen(self):
        while rclpy.ok() and not self.node.exit_event.is_set():
            try:
                incoming = await asyncio.wait_for(self.websocket.recv(), timeout=0.1)
            except asyncio.TimeoutError:
                await asyncio.sleep(0.01)
                continue
            except websockets.exceptions.ConnectionClosed:
                self.node.get_logger().warn("WebSocket connection closed by server.")
                break

            # Forward the raw JSON string from the WS server by publishing it.
            self.node.get_logger().info(f"Forwarding incoming message: {incoming}")
            ros_msg = String()
            ros_msg.data = incoming
            self.node.ws_pub.publish(ros_msg)
            await asyncio.sleep(0.01)

    async def send(self, msg):
        if self.websocket is not None:
            await self.websocket.send(msg.model_dump_json())

    async def send_image(self, cv_image):
        import cv2
        import base64

        success, encoded_img = cv2.imencode(".jpg", cv_image)
        if not success:
            self.node.get_logger().error("Failed to encode image as JPEG.")
            return

        b64_img = base64.b64encode(encoded_img.tobytes()).decode("utf-8")
        msg_obj = MessageIn(
            type=MessageInType.IMAGE,
            payload={"image_b64": b64_img},
        )
        await self.send(msg_obj)
        self.node.get_logger().debug("Sent image over WebSocket.")


###############################################################################
# WSClientNode – a dedicated ROS node that wraps the WSClient.
###############################################################################
class WSClientNode(Node):
    def __init__(self):
        super().__init__("ws_client_node")

        # Declare parameters for websocket configuration.
        self.declare_parameter("websocket_uri", "ws://localhost:8765")
        self.declare_parameter("token", "MY_HARDCODED_TOKEN")
        self.ws_uri = (
            self.get_parameter("websocket_uri").get_parameter_value().string_value
        )
        self.token = self.get_parameter("token").get_parameter_value().string_value

        # Publisher for incoming WS messages.
        self.ws_pub = self.create_publisher(String, "ws_messages", 10)

        # Subscribe to the generic outgoing topic—any message published on this topic will be sent.
        self.outgoing_sub = self.create_subscription(
            String, "ws_outgoing", self.ws_outgoing_callback, 10
        )

        self.exit_event = threading.Event()

        # Set up the WSClient.
        self.ws_client = WSClient(self.ws_uri, self.token, self)

        # Start the websocket event loop in a separate thread.
        self.ws_thread = None
        self.get_logger().info("WSClientNode initialized.")

    def ws_outgoing_callback(self, msg: String):
        """
        Callback for outgoing messages. It verifies that the received JSON has a proper message type
        and then forwards it to the WebSocket server.
        """
        try:
            data = json.loads(msg.data)
            if "type" not in data:
                self.get_logger().error(f"Outgoing message missing 'type': {msg.data}")
                return

            # Try to parse as an internal message first.
            try:
                internal_message = InternalMessage.model_validate(data)
                self.get_logger().info(f"Internal message: {internal_message}")
                if internal_message.type == InternalMessageType.READY_FOR_CONNECTION:
                    self.get_logger().info("Received ready for connection message.")
                    self.ws_thread = threading.Thread(target=self.run_ws_loop)
                    self.ws_thread.start()

            except Exception as e:
                ## That's normal, it's not an internal message.
                outgoing_message = MessageIn.model_validate(data)
                self.get_logger().info(f"Outgoing message: {outgoing_message.type}")
                asyncio.run_coroutine_threadsafe(
                    self.ws_client.send(outgoing_message), self.ws_client.loop
                )
        except Exception as e:
            self.get_logger().error(f"Error processing outgoing message: {e}")
            return

    def run_ws_loop(self):
        loop = asyncio.new_event_loop()
        self.ws_client.loop = loop
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.ws_client.connect())
        loop.close()


def main(args=None):
    rclpy.init(args=args)
    # Sleep for 1 second to allow the WSBridge to send the ready for connection message.
    time.sleep(1)
    node = WSClientNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("KeyboardInterrupt, shutting down WSClientNode.")
    finally:
        node.exit_event.set()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
