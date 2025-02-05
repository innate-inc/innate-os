import asyncio
import json
import websockets
import time
import rclpy

from brain_client.message_types import (
    MessageIn,
    MessageOut,
    MessageInType,
    MessageOutType,
)


class WSClient:
    def __init__(self, uri, token, node):
        """
        WSClient encapsulates WebSocket connection logic.
        It validates messages (using pydantic or similar) and
        dispatches them to handlers registered per message out type.
        :param uri: The websocket server URI.
        :param token: The authentication token.
        :param node: The ROS node context (for logging and access to publishers).
        """
        self.uri = uri
        self.token = token
        self.node = node
        self.websocket = None
        self.loop = None
        self.handlers = (
            {}
        )  # Dictionary to map message out types to their handler callbacks

    def register_handler(self, msg_type: MessageOutType, handler):
        """
        Register a handler for a specific message out type.
        :param msg_type: The type of the outgoing message.
        :param handler: An async function that processes the message.
        """
        self.handlers[msg_type] = handler

    async def connect(self):
        self.node.get_logger().debug(f"[WSClient] Connecting to {self.uri} ...")
        try:
            async with websockets.connect(self.uri) as websocket:
                self.websocket = websocket
                self.node.get_logger().info(f"[WSClient] Connected to {self.uri}")

                # Send auth message upon connection
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
        """
        Main loop: Receive messages from the server, validate them,
        and dispatch them to the correct handler based on message type.
        """
        while rclpy.ok() and not self.node.exit_event.is_set():
            try:
                incoming = await asyncio.wait_for(self.websocket.recv(), timeout=0.1)
            except asyncio.TimeoutError:
                await asyncio.sleep(0.01)
                continue
            except websockets.exceptions.ConnectionClosed:
                self.node.get_logger().warn("WebSocket connection closed by server.")
                break

            try:
                data = json.loads(incoming)
            except json.JSONDecodeError:
                self.node.get_logger().warn("Received non-JSON data.")
                continue

            try:
                msg = MessageOut.model_validate(data)
            except Exception as e:
                self.node.get_logger().error(f"Message validation error: {e}")
                continue

            # Dispatch the message to the registered handler based on msg.type
            handler = self.handlers.get(msg.type)
            if handler:
                await handler(msg)
            else:
                self.node.get_logger().warn(f"Unhandled message type: {msg.type}")

            await asyncio.sleep(0.01)

    async def send(self, msg):
        """
        Send a message over the WebSocket.
        The message is assumed to implement model_dump_json() (for example, a pydantic model).
        """
        if self.websocket is not None:
            await self.websocket.send(msg.model_dump_json())

    async def send_image(self, cv_image):
        """
        Encodes a JPEG image to base64 and sends it via the WebSocket.
        """
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
