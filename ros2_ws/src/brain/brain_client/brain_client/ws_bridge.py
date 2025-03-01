#!/usr/bin/env python3
import json
import rclpy
from std_msgs.msg import String

from brain_client.message_types import MessageOut, MessageOutType


class WSBridge:
    """
    WSBridge subscribes to a ROS topic (default "ws_messages") where the WSClient node publishes
    JSON-stringified messages. It parses and validates them, then dispatches to registered handlers.
    It also publishes outgoing messages (after dumping them to JSON) on a dedicated topic (default "ws_outgoing").
    """

    def __init__(
        self,
        node,
        incoming_topic: str = "ws_messages",
        outgoing_topic: str = "ws_outgoing",
    ):
        self.node = node
        self.incoming_topic = incoming_topic
        self.outgoing_topic = outgoing_topic
        self.handlers = {}  # Map from MessageOutType to handler callback

        # Subscription for incoming messages from the WSClient node.
        self.incoming_sub = self.node.create_subscription(
            String, self.incoming_topic, self._ws_callback, 10
        )
        # Publisher for outgoing messages to the WSClient node.
        self.outgoing_pub = self.node.create_publisher(String, self.outgoing_topic, 10)
        self.node.get_logger().info(
            f"WSBridge subscribed to '{self.incoming_topic}' and publishing outgoing on '{self.outgoing_topic}'."
        )

    def register_handler(self, msg_type: MessageOutType, handler):
        """
        Register a (synchronous) handler for a given message out type.
        """
        self.handlers[msg_type] = handler

    def _ws_callback(self, msg: String):
        """
        Callback for incoming JSON-string messages.
        """
        try:
            self.node.get_logger().debug(f"WSBridge: Received message: {msg.data}")
            data = json.loads(msg.data)
        except Exception as e:
            self.node.get_logger().error(f"WSBridge: Failed to parse JSON: {e}")
            return

        try:
            ws_msg = MessageOut.model_validate(data)
        except Exception as e:
            self.node.get_logger().error(f"WSBridge: Message validation error: {e}")
            return

        # Dispatch to the registered handler for this message type.
        handler = self.handlers.get(ws_msg.type)
        if handler:
            try:
                handler(ws_msg)
            except Exception as e:
                self.node.get_logger().error(
                    f"WSBridge: Handler for {ws_msg.type} raised error: {e}"
                )
        else:
            self.node.get_logger().warn(
                f"WSBridge: No handler registered for message type: {ws_msg.type}"
            )

    def send_message(self, message):
        """
        Dump the outgoing message to a JSON string and publish it on the outgoing topic.
        The 'message' is expected to be a MessageIn instance.
        """
        try:
            json_str = message.model_dump_json()
        except Exception as e:
            self.node.get_logger().error(
                f"WSBridge: Failed to dump message to JSON: {e}"
            )
            return
        outgoing_msg = String(data=json_str)
        self.outgoing_pub.publish(outgoing_msg)
