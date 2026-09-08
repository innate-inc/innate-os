# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Boot smoke: the installed brain nodes start and serve their ROS surface.

The cloud-protocol launch suites died with the cloud brain (the agent's
behavior is covered headlessly in test_local_brain.py); what still needs a
launch test is that the INSTALLED nodes actually start — a lost exec bit, a
broken import, or an undeclared runtime dependency fails here instead of at
robot boot. CI runs this under both install modes (copy and --symlink-install,
see ci/run_integration_tests.sh), which see different failure classes.

Launches the real skills server, brain client node and people node (sim mode,
no Gemini credentials — the brain reports itself unconfigured, which is part of
what we assert), then requires:
  - /brain/available_skills     -> the skills server is up and loaded a roster
  - /brain/agent_status         -> brain_client_node finished its startup
  - /brain/websocket_status     -> the brain health report is well-formed JSON
  - /brain/people               -> people_node latched its first snapshot

Run:
  colcon test --packages-select brain_client --ctest-args -R test_node_boot
  colcon test-result --verbose

NOTE: requires the ROS2 environment (run inside the ci/Dockerfile.test image).
"""

import json
import time
import unittest

import launch
import launch_ros.actions
import launch_testing.actions
import pytest
import rclpy
from brain_messages.msg import AvailableSkills
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String

LATCHED_QOS = QoSProfile(
    depth=1,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    reliability=QoSReliabilityPolicy.RELIABLE,
)


@pytest.mark.launch_test
def generate_test_description():
    brain_client = launch_ros.actions.Node(
        package="brain_client",
        executable="brain_client_node.py",
        name="brain_client",
        output="screen",
        parameters=[{"simulator_mode": True}],
    )
    skills_server = launch_ros.actions.Node(
        package="brain_client",
        executable="skills_server.py",
        name="skills_action_server",
        output="screen",
        parameters=[{"simulator_mode": True}],
    )
    people_node = launch_ros.actions.Node(
        package="brain_client",
        executable="people_node.py",
        name="people_node",
        output="screen",
        # No model download in CI: a missing face model is a health flag, and the
        # node must still come up and publish its snapshot without one.
        parameters=[{"simulator_mode": True, "allow_model_download": False, "scribe": False}],
    )
    return (
        launch.LaunchDescription(
            [
                brain_client,
                skills_server,
                people_node,
                launch_testing.actions.ReadyToTest(),
            ]
        ),
        {"brain_client": brain_client, "skills_server": skills_server, "people_node": people_node},
    )


class TestNodesBoot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rclpy.init()
        cls.node = rclpy.create_node("boot_probe")

    @classmethod
    def tearDownClass(cls):
        cls.node.destroy_node()
        rclpy.shutdown()

    def _wait_for_message(self, topic, msg_type, timeout_sec):
        messages = []
        sub = self.node.create_subscription(msg_type, topic, messages.append, LATCHED_QOS)
        deadline = time.monotonic() + timeout_sec
        while not messages and time.monotonic() < deadline:
            rclpy.spin_once(self.node, timeout_sec=0.5)
        self.node.destroy_subscription(sub)
        self.assertTrue(messages, f"nothing arrived on {topic} within {timeout_sec}s")
        return messages[0]

    def test_nodes_come_up_and_publish_their_status(self):
        # Skills server first: it gates brain_client's startup (the node waits
        # up to 60s for the roster), and its own load (torch, skill imports)
        # dominates the wall clock here.
        roster = self._wait_for_message("/brain/available_skills", AvailableSkills, timeout_sec=120.0)
        self.assertTrue(list(roster.skills), "skills server published an empty roster")

        # Latched + 3s heartbeat once brain_client_node's _startup completes.
        status = json.loads(self._wait_for_message("/brain/agent_status", String, timeout_sec=90.0).data)
        self.assertIn("brain_active", status)
        self.assertIn("current_directive", status)

        health = json.loads(self._wait_for_message("/brain/websocket_status", String, timeout_sec=30.0).data)
        # No Gemini credentials in CI: the local brain must say so truthfully
        # rather than crash or claim readiness.
        self.assertIn(health.get("backend"), ("unconfigured", "innate-proxy", "gemini-direct"))
        self.assertIn("connected", health)

        # Latched, and published as a health heartbeat every 5s even with no
        # camera in CI — so this asserts the node booted, not that it can see.
        people = json.loads(self._wait_for_message("/brain/people", String, timeout_sec=30.0).data)
        self.assertEqual(people.get("schema"), 1)
        self.assertIn("camera", people.get("health", {}))
        self.assertEqual(people.get("people"), [])
