import asyncio
import json

from rclpy.publisher import Publisher
from rclpy.subscription import Subscription
from std_msgs.msg import String
from brain_client.primitives.types import Primitive


class Nav2Controller:
    def __init__(self, nav_cmd_pub: Publisher, nav_result_sub: Subscription, logger):
        """
        This controller delegates all navigation to the dedicated NavigationNode.
        It publishes navigation commands on a topic and awaits the result published
        by the NavigationNode.
        """
        self.nav_cmd_pub = nav_cmd_pub
        self.logger = logger
        self._result_future = None

        # Set the result subscription's callback to our handler.
        nav_result_sub.callback = self._nav_result_callback

    def _nav_result_callback(self, msg: String):
        """
        Callback invoked when a navigation result is received.
        Expects a JSON payload with a "status" key.
        """
        try:
            data = json.loads(msg.data)
        except Exception as e:
            self.logger.error(f"Failed to decode navigation result: {e}")
            return

        status = data.get("status", "UNKNOWN")
        self.logger.info(f"Received navigation result: {status}")
        if self._result_future and not self._result_future.done():
            self._result_future.set_result(status)

    async def go_to_position(self, x: float, y: float, w: float = 1.0) -> str:
        """
        Publishes a navigation command and waits asynchronously for the result.

        Args:
            x (float): x-coordinate.
            y (float): y-coordinate.
            w (float): The w component of the orientation quaternion (default 1.0).

        Returns:
            str: The navigation status (e.g. "SUCCEEDED", "FAILED", etc.)
        """
        # Create an asyncio future to wait for the navigation result.
        loop = asyncio.get_running_loop()
        self._result_future = loop.create_future()

        nav_command = {
            "frame_id": "map",
            "position": {"x": x, "y": y, "z": 0.0},
            "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": w},
        }
        nav_msg = String(data=json.dumps(nav_command))
        self.nav_cmd_pub.publish(nav_msg)
        self.logger.info("Published navigation command, waiting for result...")

        # Await the result published by NavigationNode.
        status = await self._result_future
        self._result_future = None
        return status


class NavigateToPosition(Primitive):
    def __init__(self, nav_cmd_pub: Publisher, nav_result_sub: Subscription, logger):
        """
        The navigation primitive simply delegates to the Nav2Controller.
        """
        self.logger = logger
        self.nav2_controller = Nav2Controller(nav_cmd_pub, nav_result_sub, logger)

    @property
    def name(self):
        return "navigate_to_position"

    def guidelines(self):
        return "Navigate the robot to the specified position using provided x and y coordinates."

    async def execute(self, x: float, y: float):
        self.logger.info(f"Initiating navigation to position: x={x}, y={y}")
        status = await self.nav2_controller.go_to_position(x, y, w=1.0)
        if status == "SUCCEEDED":
            message = f"Reached position ({x}, {y})"
            success = True
        else:
            message = f"Failed to reach position ({x}, {y}). Status: {status}"
            success = False
        self.logger.info(f"Navigation complete. {message}")
        return message, success
