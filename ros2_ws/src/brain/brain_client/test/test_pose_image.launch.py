"""
Integration test: Pose image pipeline.

Uses the standard ROS2 launch_testing framework to:
  - Launch brain_client_node + skills_action_server with simulator_mode=True
  - Publish fake camera images and TF transforms (no hardware needed)
  - Trigger the pose-image timer via ws_messages (READY_FOR_IMAGE +
    PRIMITIVES_AND_DIRECTIVE_REGISTERED)
  - Assert that a well-formed pose_image message appears on ws_outgoing

Run with:
  colcon test --packages-select brain_client --ctest-args -R test_pose_image
  colcon test-result --verbose
"""

import base64
import json
import time
import unittest

import cv2
import launch
import launch_ros.actions
import launch_testing
import launch_testing.actions
import launch_testing.asserts
import numpy as np
import pytest
import rclpy
from geometry_msgs.msg import TransformStamped
from rclpy.qos import (
    QoSHistoryPolicy,
    QoSProfile,
    QoSReliabilityPolicy,
)
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String
from std_srvs.srv import SetBool
from tf2_ros.static_transform_broadcaster import StaticTransformBroadcaster


# -- Launch description -------------------------------------------------------

@pytest.mark.launch_test
def generate_test_description():
    """Launch brain_client_node and skills_action_server under test."""

    skills_action_server = launch_ros.actions.Node(
        package="brain_client",
        executable="skills_action_server.py",
        name="skills_action_server",
        output="screen",
        parameters=[
            {
                "simulator_mode": True,
                "image_topic": "/test/image",
                "map_topic": "/test/map",
            }
        ],
    )

    brain_client = launch_ros.actions.Node(
        package="brain_client",
        executable="brain_client_node.py",
        name="brain_client",
        output="screen",
        parameters=[
            {
                "simulator_mode": True,
                "websocket_uri": "",
                "image_topic": "/test/camera/compressed",
                "map_topic": "/test/map",
                "odom_topic": "/test/odom",
                "amcl_pose_topic": "/test/amcl_pose",
                "depth_image_topic": "/test/depth",
                "arm_camera_image_topic": "/test/arm_camera",
                "current_nav_mode_topic": "/test/nav_mode",
                "send_depth": False,
                "send_arm_camera_image": False,
                "pose_image_interval": 0.5,
            }
        ],
    )

    return (
        launch.LaunchDescription(
            [
                skills_action_server,
                brain_client,
                # launch_testing has a hard 15s timeout for process startup.
                # Fire ReadyToTest quickly; the tests themselves wait for the
                # node to be ready by polling for its services.
                launch.actions.TimerAction(
                    period=5.0,
                    actions=[launch_testing.actions.ReadyToTest()],
                ),
            ]
        ),
        {
            "skills_action_server": skills_action_server,
            "brain_client": brain_client,
        },
    )


# -- Helpers ------------------------------------------------------------------

def _make_compressed_image():
    """Create a small synthetic JPEG CompressedImage."""
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[:, :, 2] = 255  # red
    _, buf = cv2.imencode(".jpg", img)
    msg = CompressedImage()
    msg.format = "jpeg"
    msg.data = buf.tobytes()
    return msg


def _make_static_tf(x, y, yaw_z, yaw_w):
    """Build a map -> base_link static transform."""
    t = TransformStamped()
    t.header.frame_id = "map"
    t.child_frame_id = "base_link"
    t.transform.translation.x = float(x)
    t.transform.translation.y = float(y)
    t.transform.translation.z = 0.0
    t.transform.rotation.x = 0.0
    t.transform.rotation.y = 0.0
    t.transform.rotation.z = float(yaw_z)
    t.transform.rotation.w = float(yaw_w)
    return t


def _ws_msg(msg_type, payload):
    """Wrap a JSON message the way ws_client_node would publish on ws_messages."""
    s = String()
    s.data = json.dumps({"type": msg_type, "payload": payload})
    return s


# -- Active tests -------------------------------------------------------------

class TestPoseImage(unittest.TestCase):
    """Active tests that run while brain_client_node is alive."""

    @classmethod
    def setUpClass(cls):
        rclpy.init()

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def setUp(self):
        self.node = rclpy.create_node("test_pose_image")
        self.received_msgs = []

        # Subscribe to ws_outgoing to capture pose_image messages
        self.ws_sub = self.node.create_subscription(
            String, "ws_outgoing", self._ws_callback, 10
        )

        # Publisher: fake camera images
        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
        )
        self.image_pub = self.node.create_publisher(
            CompressedImage, "/test/camera/compressed", image_qos
        )

        # Publisher: ws_messages (simulate incoming WebSocket messages)
        self.ws_pub = self.node.create_publisher(String, "ws_messages", 10)

        # Static TF broadcaster: map -> base_link
        self.tf_broadcaster = StaticTransformBroadcaster(self.node)

    def tearDown(self):
        self.node.destroy_node()

    def _ws_callback(self, msg):
        self.received_msgs.append(msg.data)

    def _spin_for(self, seconds):
        """Spin the node for a given duration, processing callbacks."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(self.node, timeout_sec=0.1)

    def _wait_for_brain_client(self, timeout_sec=60.0):
        """Block until brain_client_node is fully initialized.

        Polls the /brain/set_brain_active service which is one of the last
        things created in brain_client_node.__init__.
        """
        client = self.node.create_client(SetBool, "/brain/set_brain_active")
        try:
            ready = client.wait_for_service(timeout_sec=timeout_sec)
            self.assertTrue(
                ready,
                "brain_client_node did not become ready within "
                + str(timeout_sec)
                + "s",
            )
        finally:
            self.node.destroy_client(client)

    def _trigger_pose_image(self, send_images=True, tf_x=1.0, tf_y=2.0):
        """Common setup: broadcast TF, optionally publish images, trigger timer."""
        # 1. Broadcast a static transform
        yaw_z = 0.7071
        yaw_w = 0.7071
        self.tf_broadcaster.sendTransform(
            _make_static_tf(tf_x, tf_y, yaw_z, yaw_w)
        )

        # 2. Publish fake camera images so brain_client picks them up
        if send_images:
            for _ in range(5):
                self.image_pub.publish(_make_compressed_image())
                self._spin_for(0.2)

        # 3. Send PRIMITIVES_AND_DIRECTIVE_REGISTERED
        self.ws_pub.publish(
            _ws_msg(
                "primitives_and_directive_registered",
                {"success": True, "count": 1, "directive_registered": True},
            )
        )
        self._spin_for(0.5)

        # 4. Send READY_FOR_IMAGE -- starts the pose_image timer
        self.ws_pub.publish(_ws_msg("ready_for_image", {}))
        self._spin_for(0.5)

    def _collect_pose_images(self, keep_publishing=True, max_wait=15.0):
        """Spin and optionally keep publishing images; return list of pose_image payloads."""
        results = []
        end = time.monotonic() + max_wait
        while time.monotonic() < end:
            if keep_publishing:
                self.image_pub.publish(_make_compressed_image())
            self._spin_for(0.5)

            for raw in self.received_msgs:
                data = json.loads(raw)
                if data.get("type") == "pose_image":
                    results.append(data.get("payload", {}))
            if results:
                break
        return results

    def test_pose_image_is_published(self):
        """After feeding image + TF + trigger messages, a pose_image appears."""
        self._wait_for_brain_client()
        self._trigger_pose_image()
        results = self._collect_pose_images()
        self.assertGreater(
            len(results), 0, "No pose_image message received on ws_outgoing"
        )

    def test_pose_image_payload_is_correct(self):
        """The pose_image payload contains image, x, y, theta with expected values."""
        self._wait_for_brain_client()
        self._trigger_pose_image()
        results = self._collect_pose_images()
        self.assertGreater(len(results), 0, "No pose_image message received")

        payload = results[0]

        # Verify required fields
        self.assertIn("image", payload)
        self.assertIn("x", payload)
        self.assertIn("y", payload)
        self.assertIn("theta", payload)
        self.assertIn("camera_info", payload)

        # Verify position from our TF transform
        self.assertAlmostEqual(payload["x"], 1.0, places=1)
        self.assertAlmostEqual(payload["y"], 2.0, places=1)

        # Verify theta ~ pi/2 (~ 1.5708) from the 90-degree yaw quaternion
        self.assertAlmostEqual(payload["theta"], 1.5708, delta=0.1)

        # Verify image is valid base64-encoded JPEG
        img_bytes = base64.b64decode(payload["image"])
        self.assertGreater(len(img_bytes), 0, "Image data is empty")
        np_arr = np.frombuffer(img_bytes, np.uint8)
        decoded = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        self.assertIsNotNone(decoded, "Could not decode JPEG from pose_image payload")

    def test_a_pose_image_not_sent_before_trigger(self):
        """No pose_image appears if READY_FOR_IMAGE has not been sent."""
        self._wait_for_brain_client()

        # Broadcast TF and publish images, but do NOT send trigger messages
        self.tf_broadcaster.sendTransform(
            _make_static_tf(1.0, 2.0, 0.7071, 0.7071)
        )
        for _ in range(5):
            self.image_pub.publish(_make_compressed_image())
            self._spin_for(0.2)

        # Spin for a few seconds -- timer was never started, no pose_image
        self._spin_for(3.0)

        pose_msgs = [
            raw for raw in self.received_msgs
            if json.loads(raw).get("type") == "pose_image"
        ]
        self.assertEqual(
            len(pose_msgs), 0,
            "pose_image should not be sent before READY_FOR_IMAGE trigger"
        )


# -- Post-shutdown tests ------------------------------------------------------

@launch_testing.post_shutdown_test()
class TestShutdown(unittest.TestCase):
    """Verify both nodes exited cleanly."""

    def test_exit_codes(self, proc_info):
        # Allow 0 (clean), -2 (SIGINT), and 1 (rclpy shutdown race condition
        # in brain_client_node's finally block)
        launch_testing.asserts.assertExitCodes(
            proc_info,
            allowable_exit_codes=[0, -2, 1],
        )
