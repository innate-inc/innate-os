"""
Integration test: Cross-node skill discovery and execution.

Uses the standard ROS2 launch_testing framework to:
  - Launch the skills_action_server node with real default skills
  - Run active tests that discover skills (/brain/available_skills topic)
    and execute one (ExecuteSkill action) across nodes
  - Verify clean shutdown via post-shutdown exit code checks

Run with:
  colcon test --packages-select brain_client --ctest-args -R test_skill_integration
  colcon test-result --verbose
"""

import json
import os
import time
import unittest

import launch
import launch_ros.actions
import launch_testing
import launch_testing.actions
import launch_testing.asserts
import pytest
import rclpy
from brain_messages.action import ExecuteSkill
from brain_messages.msg import AvailableSkills
from rclpy.action import ActionClient
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy

# Skills that MUST load in a headless Docker container (no hardware, no external APIs).
# These have no blocking __init__ and no missing module-level imports.
REQUIRED_SKILLS = {
    "send_email",
    "send_picture_via_email",
    "retrieve_emails",
    "arm_circle_motion",
    "arm_move_to_xyz",
    "arm_zero_position",
    "scan_for_objects",
}


@pytest.mark.launch_test
def generate_test_description():
    """Launch the skills_action_server node under test."""
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

    return (
        launch.LaunchDescription(
            [
                skills_action_server,
                # Give the node time to initialize before running tests
                launch.actions.TimerAction(
                    period=5.0,
                    actions=[launch_testing.actions.ReadyToTest()],
                ),
            ]
        ),
        {
            "skills_action_server": skills_action_server,
        },
    )


class TestSkillDiscovery(unittest.TestCase):
    """Active tests that run while the skills_action_server is alive."""

    @classmethod
    def setUpClass(cls):
        rclpy.init()

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def setUp(self):
        self.node = rclpy.create_node("test_skill_integration")

    def tearDown(self):
        self.node.destroy_node()

    def _wait_for_skills(self, timeout_sec=30.0):
        """Subscribe to /brain/available_skills and wait for a message."""
        qos = QoSProfile(
            depth=1,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            reliability=QoSReliabilityPolicy.RELIABLE,
        )
        received = []

        def callback(msg):
            received.append(msg)

        sub = self.node.create_subscription(
            AvailableSkills, "/brain/available_skills", callback, qos
        )
        try:
            deadline = time.time() + timeout_sec
            while not received and time.time() < deadline:
                rclpy.spin_once(self.node, timeout_sec=0.5)
            self.assertTrue(received, "No AvailableSkills message received")
            return received[0]
        finally:
            self.node.destroy_subscription(sub)

    def test_default_skills_are_discovered(self):
        """AvailableSkills topic returns at least the required default skills."""
        msg = self._wait_for_skills()
        skill_names = {s.name for s in msg.skills}

        self.assertGreaterEqual(
            len(msg.skills),
            len(REQUIRED_SKILLS),
            f"Expected at least {len(REQUIRED_SKILLS)} skills, got {len(msg.skills)}: {sorted(skill_names)}",
        )

        missing = REQUIRED_SKILLS - skill_names
        self.assertFalse(
            missing,
            f"Missing required skills: {sorted(missing)}. Available: {sorted(skill_names)}",
        )

        # Verify they are all code type
        for skill in msg.skills:
            if skill.name in REQUIRED_SKILLS:
                self.assertEqual(
                    skill.type,
                    "code",
                    f"Skill '{skill.name}' should be type 'code', got '{skill.type}'",
                )

    def test_send_email_has_correct_inputs(self):
        """send_email skill exposes subject, message, and recipients parameters."""
        msg = self._wait_for_skills()
        send_email = next((s for s in msg.skills if s.name == "send_email"), None)

        self.assertIsNotNone(send_email, "send_email skill not found")
        inputs = json.loads(send_email.inputs_json) if send_email.inputs_json else {}
        self.assertIn("subject", inputs)
        self.assertIn("message", inputs)
        self.assertIn("recipients", inputs)

    def test_execute_send_email_succeeds(self):
        """Executing send_email via the ExecuteSkill action returns success."""
        action_client = ActionClient(self.node, ExecuteSkill, "execute_skill")
        try:
            self.assertTrue(
                action_client.wait_for_server(timeout_sec=30.0),
                "ExecuteSkill action server not available",
            )

            goal = ExecuteSkill.Goal()
            goal.skill_type = "send_email"
            goal.inputs = json.dumps(
                {
                    "subject": "Integration Test",
                    "message": "This is an automated integration test.",
                    "recipients": ["test@example.com"],
                }
            )

            send_goal_future = action_client.send_goal_async(goal)
            rclpy.spin_until_future_complete(self.node, send_goal_future, timeout_sec=10.0)
            self.assertTrue(send_goal_future.done(), "send_goal timed out")

            goal_handle = send_goal_future.result()
            self.assertTrue(goal_handle.accepted, "Goal was rejected by action server")

            result_future = goal_handle.get_result_async()
            rclpy.spin_until_future_complete(self.node, result_future, timeout_sec=15.0)
            self.assertTrue(result_future.done(), "get_result timed out")

            result = result_future.result().result

            self.assertTrue(result.success, f"send_email failed: {result.message}")
            self.assertEqual(result.success_type, "success")
            self.assertEqual(result.skill_type, "send_email")
            self.assertIn("Email sent to", result.message)
        finally:
            action_client.destroy()


@launch_testing.post_shutdown_test()
class TestShutdown(unittest.TestCase):
    """Verify the skills_action_server exited cleanly."""

    def test_exit_codes(self, proc_info):
        # Allow exit code 0 (clean) and -2 (SIGINT, normal for Python ROS nodes)
        launch_testing.asserts.assertExitCodes(
            proc_info,
            allowable_exit_codes=[0, -2],
        )
