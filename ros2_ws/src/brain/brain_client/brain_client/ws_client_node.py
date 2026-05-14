#!/usr/bin/env python3
import asyncio
import json
import threading
import time
from typing import Optional

import rclpy
from rclpy.node import Node
from std_msgs.msg import String

import websockets

# Import your message definitions
from brain_client.message_types import (
    InternalMessage,
    InternalMessageType,
    MessageIn,
    MessageInType,
)


PLACEHOLDER_SERVICE_KEYS = {
    "my_hardcoded_token",
    "your_service_key_here",
    "your-service-key-here",
    "replace_me",
    "changeme",
    "change_me",
    "todo",
}

CONNECTED_GRACE_SECONDS = 2.0


###############################################################################
# WSClient class – largely unchanged, but now it relies on its node to publish.
###############################################################################
class WSClient:
    def __init__(self, uri, token, node, robot_version: Optional[str] = None):
        """
        :param node: A reference to the ROS node, used for logging and publishing.
        :param robot_version: Version string from /robot/info to send with auth.
        """
        self.uri = uri
        self.token = token
        self.node = node
        self.robot_version = robot_version
        self.websocket = None
        self.loop = None

    async def connect(self):
        """Connect to the WebSocket server with automatic reconnection."""
        reconnect_delay = 1  # Start with 1 second delay
        max_reconnect_delay = 30  # Max 30 seconds between retries
        
        while rclpy.ok() and not self.node.exit_event.is_set():
            disconnect_reason = "Connection lost."
            status_reported = False
            self.node.get_logger().info(f"[WSClient] Connecting to {self.uri} ...")
            self.node.set_ws_status(
                "connecting", False, f"Connecting to {self.uri}"
            )
            try:
                # Set a longer timeout that works for all operations
                async with websockets.connect(
                    self.uri,
                    ping_interval=30,  # Send a ping every 30 seconds
                    ping_timeout=60,  # Wait up to 60 seconds for a pong response
                ) as websocket:
                    self.websocket = websocket
                    self.node.set_ws_status(
                        "authenticating",
                        False,
                        f"Socket opened to {self.uri}; authenticating.",
                    )
                    reconnect_delay = 1  # Reset delay on successful connection

                    # Send auth message upon connection.
                    auth_payload = {"token": self.token}
                    if self.robot_version:
                        auth_payload["client_version"] = self.robot_version
                    auth_msg = MessageIn(
                        type=MessageInType.AUTH,
                        payload=auth_payload,
                    )
                    await self.send(auth_msg)
                    self.node.get_logger().debug(f"[WSClient] Auth message sent with version: {self.robot_version}")
                    self.node.set_ws_status(
                        "authenticating",
                        False,
                        "Auth message sent; waiting for backend confirmation.",
                    )

                    disconnect_reason = await self.listen()
            except Exception as e:
                disconnect_reason = f"Connection error: {e}"
                self.node.set_ws_status("connection_error", False, disconnect_reason)
                status_reported = True
            
            self.websocket = None
            
            # Check if we should exit before attempting reconnection
            if self.node.exit_event.is_set() or not rclpy.ok():
                break
                
            self.node.get_logger().warn(
                f"[WSClient] {disconnect_reason} Reconnecting in {reconnect_delay}s..."
            )
            if not status_reported:
                self.node.set_ws_status(
                    "disconnected",
                    False,
                    f"{disconnect_reason} Reconnecting in {reconnect_delay}s.",
                )
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)  # Exponential backoff

        self.node.get_logger().info("[WSClient] Stopping WSClient.")
        self.websocket = None
        self.node.set_ws_status("stopped", False, "WebSocket client stopped.")

    async def listen(self):
        connected_started_at = time.time()
        connected_reported = False

        while rclpy.ok() and not self.node.exit_event.is_set():
            try:
                # Use longer timeout to reduce CPU spin; we'll wake immediately on message
                incoming = await asyncio.wait_for(self.websocket.recv(), timeout=1.0)
            except asyncio.TimeoutError:
                if (
                    not connected_reported
                    and time.time() - connected_started_at >= CONNECTED_GRACE_SECONDS
                ):
                    self.node.get_logger().info(f"[WSClient] Connected to {self.uri}")
                    self.node.set_ws_status(
                        "connected", True, f"Connected to {self.uri}"
                    )
                    connected_reported = True
                # No message received, just continue the loop (no extra sleep needed)
                continue
            except websockets.exceptions.ConnectionClosed as e:
                reason = e.reason or "no reason"
                message = (
                    f"WebSocket connection closed by server "
                    f"(code={e.code}, reason={reason})."
                )
                if (
                    reason == "no reason"
                    and self.node._last_ws_error_message
                    and time.time() - self.node._last_ws_error_at < 60.0
                ):
                    message = f"{message} Last backend error: {self.node._last_ws_error_message}"
                return message

            # Check for error messages (especially version_mismatch)
            try:
                data = json.loads(incoming)
                msg_type = data.get("type")
                normalized_type = (
                    msg_type.rsplit("/", 1)[-1] if isinstance(msg_type, str) else msg_type
                )
                if normalized_type == "error":
                    payload = data.get("payload", {})
                    error_type = payload.get("error", "unknown")
                    message = payload.get("message", "Unknown error")
                    self.node._handle_ws_error(error_type, message)
            except json.JSONDecodeError:
                pass  # Not valid JSON, just forward it

            if (
                not connected_reported
                and time.time() - connected_started_at >= CONNECTED_GRACE_SECONDS
            ):
                self.node.get_logger().info(f"[WSClient] Connected to {self.uri}")
                self.node.set_ws_status("connected", True, f"Connected to {self.uri}")
                connected_reported = True

            # Forward the raw JSON string from the WS server by publishing it.
            self.node.get_logger().debug(f"Forwarding incoming message: {incoming}")
            ros_msg = String()
            ros_msg.data = incoming
            self.node.ws_pub.publish(ros_msg)

        return "WebSocket listener stopped."

    async def send(self, msg):
        if self.websocket is not None:
            await self.websocket.send(msg.model_dump_json())


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
        self.exit_event = threading.Event()

        self._ws_status_pub = self.create_publisher(
            String, "/brain/websocket_status", 10
        )
        self._ws_status = {
            "state": "starting",
            "connected": False,
            "message": "WebSocket client starting.",
            "uri": self.ws_uri,
            "hosted": False,
            "timestamp": time.time(),
        }
        self._ws_status_timer = self.create_timer(2.0, self._publish_ws_status)
        self._last_backend_unavailable_log_key = None
        self._last_backend_unavailable_log_at = 0.0
        self._last_invalid_config_log_at = 0.0
        self._last_ws_error_message = ""
        self._last_ws_error_at = 0.0

        # Validate websocket URI early
        self._ws_configured = self._validate_ws_uri(self.ws_uri)
        if not self._ws_configured:
            self._log_invalid_config_once(
                "❌ WebSocket URI not configured or invalid. Set 'websocket_uri' "
                "parameter (must start with ws:// or wss://)."
            )
        self._hosted_innate_uri = self._is_hosted_innate_uri(self.ws_uri)
        self._token_configured = self._validate_token_for_uri(self.ws_uri, self.token)
        if not self._token_configured:
            self._log_invalid_config_once(
                "❌ Missing or placeholder INNATE_SERVICE_KEY for hosted Innate agent. "
                "Set a real service key before connecting to the hosted agent."
            )
        if not self._ws_configured:
            self.set_ws_status(
                "invalid_config",
                False,
                "WebSocket URI is not configured or invalid.",
            )
        elif not self._token_configured:
            self.set_ws_status(
                "invalid_config",
                False,
                "Missing or placeholder INNATE_SERVICE_KEY for hosted Innate agent.",
            )
        else:
            self.set_ws_status(
                "configured",
                False,
                "WebSocket configured; waiting for connection request.",
            )

        # Robot version from /robot/info
        self._robot_version: Optional[str] = None
        self._robot_info_sub = self.create_subscription(
            String, "/robot/info", self._robot_info_callback, 10
        )

        # Publisher for TTS messages
        self._tts_pub = self.create_publisher(String, "/brain/tts", 10)

        # Publisher for incoming WS messages.
        self.ws_pub = self.create_publisher(String, "ws_messages", 10)

        # Subscribe to the generic outgoing topic—any message published on this topic will be sent.
        self.outgoing_sub = self.create_subscription(
            String, "ws_outgoing", self.ws_outgoing_callback, 10
        )

        # Set up the WSClient (only if configured).
        if self._ws_configured and self._token_configured:
            self.ws_client = WSClient(self.ws_uri, self.token, self, self._robot_version)
        else:
            self.ws_client = None

        # Start the websocket event loop in a separate thread.
        self.ws_thread = None
        self.get_logger().info("WSClientNode initialized.")

    def set_ws_status(self, state: str, connected: bool, message: str):
        """Update and publish cloud/local-agent websocket status."""
        self._ws_status = {
            "state": state,
            "connected": connected,
            "message": message,
            "uri": self.ws_uri,
            "hosted": self._hosted_innate_uri,
            "timestamp": time.time(),
        }
        self._publish_ws_status()
        self._log_backend_unavailable_once(state, connected, message)

    def _log_backend_unavailable_once(
        self, state: str, connected: bool, message: str
    ):
        if connected or state in {"starting", "configured", "connecting", "authenticating"}:
            return

        now = time.time()
        key = (state, message)
        if (
            key == self._last_backend_unavailable_log_key
            and now - self._last_backend_unavailable_log_at < 30.0
        ):
            return

        self._last_backend_unavailable_log_key = key
        self._last_backend_unavailable_log_at = now
        self.get_logger().error(
            "[WSClient] Brain backend unavailable "
            f"(state={state}, uri={self.ws_uri}): {message}"
        )

    def _log_invalid_config_once(self, message: str):
        now = time.time()
        if now - self._last_invalid_config_log_at < 30.0:
            return
        self._last_invalid_config_log_at = now
        self.get_logger().error(message)

    def _publish_ws_status(self):
        try:
            msg = String()
            msg.data = json.dumps(self._ws_status)
            self._ws_status_pub.publish(msg)
        except Exception as e:
            self.get_logger().debug(f"Failed to publish websocket status: {e}")

    def _robot_info_callback(self, msg: String):
        """Callback to extract robot version from /robot/info."""
        try:
            data = json.loads(msg.data)
            version = data.get("version")
            if version and version != self._robot_version:
                self._robot_version = version
                self.get_logger().info(f"Robot version: {version}")
                # Update ws_client version if it exists
                if self.ws_client:
                    self.ws_client.robot_version = version
        except Exception as e:
            self.get_logger().debug(f"Error parsing robot info: {e}")

    def _speak_error(self, message: str):
        """Publish error message to TTS topic."""
        try:
            tts_msg = String()
            tts_msg.data = message
            self._tts_pub.publish(tts_msg)
            self.get_logger().info(f"Speaking error: {message}")
        except Exception as e:
            self.get_logger().error(f"Error publishing to TTS: {e}")

    def _handle_ws_error(self, error_type: str, message: str):
        """Handle WebSocket error messages."""
        self._last_ws_error_message = f"{error_type}: {message}"
        self._last_ws_error_at = time.time()
        if error_type == "version_mismatch":
            self._speak_error(message)
            self.get_logger().error(f"Version mismatch: {message}")
            self.set_ws_status("backend_error", False, f"Version mismatch: {message}")
        else:
            self.get_logger().error(f"WebSocket error: {error_type} - {message}")
            self.set_ws_status(
                "backend_error", False, f"Backend error ({error_type}): {message}"
            )

    def _validate_ws_uri(self, uri: str) -> bool:
        """Check if the websocket URI is valid."""
        if not uri or not uri.strip():
            return False
        return uri.startswith("ws://") or uri.startswith("wss://")

    def _is_hosted_innate_uri(self, uri: str) -> bool:
        return uri.startswith("wss://agent-v1.innate.bot") or uri.startswith(
            "wss://brain.innate.bot"
        )

    def _validate_token_for_uri(self, uri: str, token: str) -> bool:
        if not self._is_hosted_innate_uri(uri):
            return True
        normalized = (token or "").strip()
        if not normalized:
            return False
        return normalized.lower() not in PLACEHOLDER_SERVICE_KEYS

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

            # Check the message type directly before attempting validation
            message_type = data.get("type")

            # Check if it's an internal message type
            if message_type in [t.value for t in InternalMessageType]:
                internal_message = InternalMessage.model_validate(data)
                self.get_logger().info(f"Internal message: {internal_message}")
                if internal_message.type == InternalMessageType.READY_FOR_CONNECTION:
                    if not self._ws_configured:
                        self._log_invalid_config_once(
                            "WebSocket not configured, ignoring connection request."
                        )
                        self.set_ws_status(
                            "invalid_config",
                            False,
                            "WebSocket URI is not configured or invalid.",
                        )
                        return
                    if not self._token_configured:
                        self._log_invalid_config_once(
                            "Hosted Innate agent selected but INNATE_SERVICE_KEY is missing or a placeholder. "
                            "Skipping websocket connection."
                        )
                        self.set_ws_status(
                            "invalid_config",
                            False,
                            "Missing or placeholder INNATE_SERVICE_KEY for hosted Innate agent.",
                        )
                        return
                    self.get_logger().debug("Received ready for connection message.")
                    if self.ws_thread is None or not self.ws_thread.is_alive():
                        self.ws_thread = threading.Thread(target=self.run_ws_loop)
                        self.ws_thread.start()
                    else:
                        self.get_logger().debug("WebSocket thread is already running.")
            # Otherwise, assume it's a regular outgoing message
            elif message_type in [t.value for t in MessageInType]:
                outgoing_message = MessageIn.model_validate(data)
                self.get_logger().debug(f"Outgoing message: {outgoing_message.type}")

                # Check if the loop exists and is running before trying to send
                if not self.ws_client:
                    self._log_invalid_config_once(
                        "Cannot send websocket message: hosted Innate agent is not configured "
                        "(missing or placeholder INNATE_SERVICE_KEY)."
                    )
                elif self.ws_client.loop and self.ws_client.loop.is_running():
                    try:
                        asyncio.run_coroutine_threadsafe(
                            self.ws_client.send(outgoing_message), self.ws_client.loop
                        )
                    except RuntimeError as e:
                        self.get_logger().error(f"Error sending message: {e}")
                else:
                    self.get_logger().warn(
                        "Cannot send message: WebSocket or event loop not available"
                    )
            else:
                self.get_logger().error(f"Unknown message type: {message_type}")

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
