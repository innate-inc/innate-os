#!/usr/bin/env python3
import pytest
import sys
import os
import numpy as np
import cv2
from unittest.mock import MagicMock


# Create mocks for all the ROS2 dependencies
class MockNode:
    def __init__(self, name):
        self.name = name
        self.parameters = {}
        self.publishers = {}
        self.subscriptions = {}
        self.services = {}
        self.timers = {}
        self.logger = MagicMock()

    def get_logger(self):
        return self.logger

    def create_publisher(self, msg_type, topic, qos):
        self.publishers[topic] = MagicMock()
        return self.publishers[topic]

    def create_subscription(self, msg_type, topic, callback, qos):
        self.subscriptions[topic] = callback
        return MagicMock()

    def create_service(self, srv_type, name, callback):
        self.services[name] = callback
        return MagicMock()

    def create_timer(self, period, callback):
        timer = MagicMock()
        self.timers[callback] = timer
        return timer

    def declare_parameter(self, name, default_value):
        self.parameters[name] = default_value

    def get_parameter(self, name):
        value = self.parameters.get(name)
        return MagicMock(
            get_parameter_value=lambda: MagicMock(
                string_value=value if isinstance(value, str) else "",
                bool_value=bool(value),
                integer_value=int(value) if isinstance(value, (int, float)) else 0,
                double_value=float(value) if isinstance(value, (int, float)) else 0.0,
            )
        )

    def destroy_node(self):
        # Mock implementation of destroy_node
        return None


class MockOdometry:
    class Pose:
        class PoseData:
            def __init__(self, x=0.0, y=0.0, z=0.0, qw=1.0, qx=0.0, qy=0.0, qz=0.0):
                self.position = type("Position", (), {"x": x, "y": y, "z": z})
                self.orientation = type(
                    "Orientation", (), {"w": qw, "x": qx, "y": qy, "z": qz}
                )

        def __init__(self):
            self.pose = self.PoseData()

    def __init__(self):
        self.pose = self.Pose()


class MockCompressedImage:
    def __init__(self, data=None):
        if data is None:
            img = np.zeros((100, 100, 3), dtype=np.uint8)
            img[:, :, 2] = 255  # Red color
            _, encoded_img = cv2.imencode(".jpg", img)
            self.data = encoded_img.tobytes()
        else:
            self.data = data


class MockWSBridge:
    def __init__(
        self, node, incoming_topic="ws_messages", outgoing_topic="ws_outgoing"
    ):
        self.node = node
        self.incoming_topic = incoming_topic
        self.outgoing_topic = outgoing_topic
        self.handlers = {}
        self.sent_messages = []

    def register_handler(self, msg_type, handler):
        self.handlers[msg_type] = handler

    def send_message(self, message):
        self.sent_messages.append(message)


class MockPrimitive:
    def __init__(self, name, guidelines_text="Test guideline"):
        self._name = name
        self._guidelines = guidelines_text

    @property
    def name(self):
        return self._name

    def guidelines(self):
        return self._guidelines

    def execute(self, *args, **kwargs):
        return f"Executed {self._name}", True


class MockDirective:
    def __init__(self, name, primitives=None, prompt=None):
        self.name = name
        self._primitives = primitives or []
        self._prompt = prompt

    def get_primitives(self):
        return self._primitives

    def get_prompt(self):
        return self._prompt


sys.modules["rclpy"] = MagicMock()
sys.modules["rclpy.node"] = MagicMock()
sys.modules["rclpy.node"].Node = MockNode
sys.modules["rclpy.action"] = MagicMock()
sys.modules["rclpy.duration"] = MagicMock()
sys.modules["rclpy.qos"] = MagicMock()
sys.modules["tf2_ros"] = MagicMock()
sys.modules["tf2_ros.buffer"] = MagicMock()
sys.modules["tf2_ros.transform_listener"] = MagicMock()
sys.modules["sensor_msgs"] = MagicMock()
sys.modules["sensor_msgs.msg"] = MagicMock()
sys.modules["sensor_msgs.msg"].CompressedImage = MockCompressedImage
sys.modules["sensor_msgs.msg"].Image = MagicMock()
sys.modules["geometry_msgs"] = MagicMock()
sys.modules["geometry_msgs.msg"] = MagicMock()
sys.modules["geometry_msgs.msg"].Twist = MagicMock()
sys.modules["std_msgs"] = MagicMock()
sys.modules["std_msgs.msg"] = MagicMock()
sys.modules["std_msgs.msg"].String = MagicMock()
sys.modules["nav_msgs"] = MagicMock()
sys.modules["nav_msgs.msg"] = MagicMock()
sys.modules["nav_msgs.msg"].Odometry = MockOdometry
sys.modules["std_srvs"] = MagicMock()
sys.modules["std_srvs.srv"] = MagicMock()
sys.modules["std_srvs.srv"].SetBool = MagicMock()
sys.modules["brain_messages"] = MagicMock()
sys.modules["brain_messages.srv"] = MagicMock()
sys.modules["brain_messages.srv"].GetChatHistory = MagicMock()
sys.modules["brain_messages.srv"].GetAvailableDirectives = MagicMock()
sys.modules["brain_messages.action"] = MagicMock()
sys.modules["brain_messages.action"].ExecutePrimitive = MagicMock()
sys.modules["brain_messages.action"].ExecutePrimitive.Goal = MagicMock
sys.modules["nav2_simple_commander"] = MagicMock()
sys.modules["nav2_simple_commander.robot_navigator"] = MagicMock()
sys.modules["nav2_simple_commander.robot_navigator"].BasicNavigator = MagicMock()
sys.modules["nav2_simple_commander.robot_navigator"].TaskResult = MagicMock()


# Mock the primitives
mock_navigate = MockPrimitive("navigate_to_position", "Navigate to a position")
mock_email = MockPrimitive("send_email", "Send an email")
sys.modules["brain_client.primitives.navigate_to_position"] = MagicMock()
sys.modules["brain_client.primitives.navigate_to_position"].NavigateToPosition = (
    lambda logger: mock_navigate
)
sys.modules["brain_client.primitives.send_email"] = MagicMock()
sys.modules["brain_client.primitives.send_email"].SendEmail = lambda logger: mock_email

# Mock the directives
mock_default = MockDirective(
    "default_directive", ["navigate_to_position"], "Default directive"
)
mock_sassy = MockDirective(
    "sassy_directive", ["navigate_to_position"], "Sassy directive"
)
mock_friendly = MockDirective(
    "friendly_guide_directive", ["navigate_to_position"], "Friendly guide directive"
)
mock_security = MockDirective(
    "security_patrol_directive", ["navigate_to_position"], "Security patrol directive"
)
mock_elder = MockDirective(
    "elder_safety_directive",
    ["navigate_to_position", "send_email"],
    "Elder safety directive",
)

sys.modules["brain_client.directives.default_directive"] = MagicMock()
sys.modules["brain_client.directives.default_directive"].DefaultDirective = (
    lambda: mock_default
)
sys.modules["brain_client.directives.sassy_directive"] = MagicMock()
sys.modules["brain_client.directives.sassy_directive"].SassyDirective = (
    lambda: mock_sassy
)
sys.modules["brain_client.directives.friendly_guide_directive"] = MagicMock()
sys.modules[
    "brain_client.directives.friendly_guide_directive"
].FriendlyGuideDirective = lambda: mock_friendly
sys.modules["brain_client.directives.security_patrol_directive"] = MagicMock()
sys.modules[
    "brain_client.directives.security_patrol_directive"
].SecurityPatrolDirective = lambda: mock_security
sys.modules["brain_client.directives.elder_safety_directive"] = MagicMock()
sys.modules["brain_client.directives.elder_safety_directive"].ElderSafetyDirective = (
    lambda: mock_elder
)

# Mock the WSBridge
sys.modules["brain_client.ws_bridge"] = MagicMock()
sys.modules["brain_client.ws_bridge"].WSBridge = MockWSBridge

# Add the parent directory to the path so we can import the brain_client module
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from brain_client.brain_client_node import BrainClientNode
from brain_client.message_types import (
    MessageInType,
    MessageOut,
    MessageOutType,
)


# Now we can import the BrainClientNode


def test_brain_client_node_initialization():
    """Test BrainClientNode initializes correctly with the pose image feature."""
    # Create a BrainClientNode instance
    node = BrainClientNode()

    # Check that the pose image parameters are initialized correctly
    assert node.pose_image_interval == 0.5
    assert node.pose_image_timer is None
    assert node.pose_image_started is False


def test_pose_image_flow():
    """Test the complete flow of the pose image feature."""
    # Create a BrainClientNode instance
    node = BrainClientNode()

    # Set up test data
    test_img = np.zeros((100, 100, 3), dtype=np.uint8)
    test_img[:, :, 2] = 255  # Red color
    node.last_image = test_img

    # Set up test odometry data
    node.last_odom = MockOdometry()
    node.last_odom.pose.pose.position.x = 1.0
    node.last_odom.pose.pose.position.y = 2.0
    node.last_odom.pose.pose.orientation.w = 0.7071
    node.last_odom.pose.pose.orientation.z = 0.7071  # 90 degrees rotation

    # Reset the sent messages
    node.ws_bridge.sent_messages = []

    # Step 1: Simulate primitives registration
    reg_msg = MessageOut(
        type=MessageOutType.PRIMITIVES_AND_DIRECTIVE_REGISTERED,
        payload={"success": True, "count": 1, "directive_registered": True},
    )
    node._handle_primitives_and_directive_registered(reg_msg)

    # Verify primitives are registered
    assert node.primitives_registered is True

    # Step 2: Simulate receiving ready_for_image message
    ready_msg = MessageOut(
        type=MessageOutType.READY_FOR_IMAGE,
        payload={},
    )
    node._handle_ready_for_image(ready_msg)

    # Verify ready_for_image flag is set
    assert node.ready_for_image is True

    # Verify pose image timer is started
    assert node.pose_image_started is True
    assert node.pose_image_timer is not None

    # Step 3: Reset the sent messages
    node.ws_bridge.sent_messages = []

    # Step 4: Manually trigger the pose image callback
    node.pose_image_callback()

    # Step 5: Verify a pose_image message was sent
    assert len(node.ws_bridge.sent_messages) == 1
    sent_message = node.ws_bridge.sent_messages[0]

    # Verify it's a pose_image message
    assert sent_message.type == MessageInType.POSE_IMAGE

    # Verify the message contains the expected fields
    assert "image" in sent_message.payload
    assert "x" in sent_message.payload
    assert "y" in sent_message.payload
    assert "theta" in sent_message.payload

    # Verify the position data matches our mock odometry
    assert sent_message.payload["x"] == 1.0
    assert sent_message.payload["y"] == 2.0

    # Calculate expected theta from quaternion
    # siny_cosp = 2.0 * (0.7071 * 0.7071 + 0.0 * 0.0)
    # cosy_cosp = 1.0 - 2.0 * (0.0 * 0.0 + 0.7071 * 0.7071)
    # expected_theta = math.atan2(siny_cosp, cosy_cosp)
    # This should be approximately pi/2 (1.57)
    assert abs(sent_message.payload["theta"] - 1.57) < 0.1

    # Step 6: Clean up
    node.destroy_node()

    # Verify the timer was cancelled
    node.pose_image_timer.cancel.assert_called_once()


def test_pose_image_starts_after_registration():
    """Test pose image timer starts after ready_for_image and primitives reg."""
    # Create a BrainClientNode instance
    node = BrainClientNode()

    # Set ready_for_image but not primitives_registered
    node.ready_for_image = True
    node.primitives_registered = False

    # Simulate receiving ready_for_image message
    ready_msg = MessageOut(
        type=MessageOutType.READY_FOR_IMAGE,
        payload={},
    )
    node._handle_ready_for_image(ready_msg)

    # Verify pose image timer is NOT started yet
    assert node.pose_image_started is False
    assert node.pose_image_timer is None

    # Now simulate primitives registration
    reg_msg = MessageOut(
        type=MessageOutType.PRIMITIVES_AND_DIRECTIVE_REGISTERED,
        payload={"success": True, "count": 1, "directive_registered": True},
    )
    node._handle_primitives_and_directive_registered(reg_msg)

    # Verify pose image timer is now started
    assert node.pose_image_started is True
    assert node.pose_image_timer is not None

    # Clean up
    node.destroy_node()


def test_pose_image_starts_after_ready_for_image():
    """Test pose image timer starts after primitives reg and ready_for_image."""
    # Create a BrainClientNode instance
    node = BrainClientNode()

    # Set primitives_registered but not ready_for_image
    node.ready_for_image = False
    node.primitives_registered = True

    # Simulate primitives registration
    reg_msg = MessageOut(
        type=MessageOutType.PRIMITIVES_AND_DIRECTIVE_REGISTERED,
        payload={"success": True, "count": 1, "directive_registered": True},
    )
    node._handle_primitives_and_directive_registered(reg_msg)

    # Verify pose image timer is NOT started yet
    assert node.pose_image_started is False
    assert node.pose_image_timer is None

    # Now simulate receiving ready_for_image message
    ready_msg = MessageOut(
        type=MessageOutType.READY_FOR_IMAGE,
        payload={},
    )
    node._handle_ready_for_image(ready_msg)

    # Verify pose image timer is now started
    assert node.pose_image_started is True
    assert node.pose_image_timer is not None

    # Clean up
    node.destroy_node()


def test_pose_image_skips_when_data_missing():
    """Test pose image callback skips when image or odom data is missing."""
    # Create a BrainClientNode instance
    node = BrainClientNode()

    # Set up the test with missing image
    node.last_image = None
    node.last_odom = MockOdometry()

    # Reset the sent messages
    node.ws_bridge.sent_messages = []

    # Call the pose image callback
    node.pose_image_callback()

    # Verify no message was sent
    assert len(node.ws_bridge.sent_messages) == 0

    # Set up the test with missing odometry
    node.last_image = np.zeros((100, 100, 3), dtype=np.uint8)
    node.last_odom = None

    # Call the pose image callback
    node.pose_image_callback()

    # Verify no message was sent
    assert len(node.ws_bridge.sent_messages) == 0

    # Clean up
    node.destroy_node()


if __name__ == "__main__":
    pytest.main(["-xvs", __file__])
