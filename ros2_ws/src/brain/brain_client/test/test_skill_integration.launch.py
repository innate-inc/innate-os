"""
Integration test: Cross-node skill discovery and execution.

Uses the standard ROS2 launch_testing framework to:
  - Launch the skills_action_server node with real default skills
  - Run active tests that discover skills (GetAvailableSkills service)
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
from brain_messages.srv import GetAvailableSkills
from rclpy.action import ActionClient

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

    def test_default_skills_are_discovered(self):
        """GetAvailableSkills returns at least the required default skills."""
        client = self.node.create_client(GetAvailableSkills, "/brain/get_available_skills")
        try:
            self.assertTrue(
                client.wait_for_service(timeout_sec=30.0),
                "GetAvailableSkills service not available",
            )

            request = GetAvailableSkills.Request()
            request.include_in_training = False

            future = client.call_async(request)
            rclpy.spin_until_future_complete(self.node, future, timeout_sec=10.0)
            self.assertTrue(future.done(), "GetAvailableSkills call timed out")

            response = future.result()
            skills = json.loads(response.skills_json)
            skill_names = {s["name"] for s in skills}

            self.assertGreaterEqual(
                len(skills),
                len(REQUIRED_SKILLS),
                f"Expected at least {len(REQUIRED_SKILLS)} skills, got {len(skills)}: {sorted(skill_names)}",
            )

            missing = REQUIRED_SKILLS - skill_names
            self.assertFalse(
                missing,
                f"Missing required skills: {sorted(missing)}. Available: {sorted(skill_names)}",
            )

            # Verify they are all code type
            for skill in skills:
                if skill["name"] in REQUIRED_SKILLS:
                    self.assertEqual(
                        skill.get("type"),
                        "code",
                        f"Skill '{skill['name']}' should be type 'code', got '{skill.get('type')}'",
                    )
        finally:
            self.node.destroy_client(client)

    def test_send_email_has_correct_inputs(self):
        """send_email skill exposes subject, message, and recipients parameters."""
        client = self.node.create_client(GetAvailableSkills, "/brain/get_available_skills")
        try:
            self.assertTrue(client.wait_for_service(timeout_sec=30.0))

            request = GetAvailableSkills.Request()
            future = client.call_async(request)
            rclpy.spin_until_future_complete(self.node, future, timeout_sec=10.0)
            self.assertTrue(future.done())

            skills = json.loads(future.result().skills_json)
            send_email = next((s for s in skills if s["name"] == "send_email"), None)

            self.assertIsNotNone(send_email, "send_email skill not found")
            inputs = send_email.get("inputs", {})
            self.assertIn("subject", inputs)
            self.assertIn("message", inputs)
            self.assertIn("recipients", inputs)
        finally:
            self.node.destroy_client(client)

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
