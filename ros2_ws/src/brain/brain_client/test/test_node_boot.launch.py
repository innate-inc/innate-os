# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Boot smoke: the installed brain nodes start and serve their ROS surface.

The cloud-protocol launch suites died with the cloud brain (the agent's
behavior is covered headlessly in test_local_brain.py); what still needs a
launch test is that the INSTALLED nodes actually start — a lost exec bit, a
broken import, or an undeclared runtime dependency fails here instead of at
robot boot. CI runs this under both install modes (copy and --symlink-install,
see ci/run_integration_tests.sh), which see different failure classes.

Launches the real skills server and brain client node from each installed
production launch profile, with simulator_mode enabled and auxiliary hardware
nodes excluded. The brain remains inactive. Requires:
  - /brain/available_skills     -> the skills server is up and loaded a roster
  - /brain/agent_status         -> brain_client_node finished its startup
  - /brain/websocket_status     -> the brain health report is well-formed JSON
  - distinct brain/helper names and repeated reads of declared parameters

Run:
  colcon test --packages-select brain_client --ctest-args -R test_node_boot
  colcon test-result --verbose

NOTE: requires the ROS2 environment (run inside the ci/Dockerfile.test image).
"""

import importlib.util
import json
import time
import unittest
from pathlib import Path

import launch
import launch_ros.actions
import launch_testing.actions
import pytest
import rclpy
from ament_index_python.packages import get_package_share_directory
from brain_messages.msg import AvailableSkills
from launch.actions import SetLaunchConfiguration
from launch.utilities import normalize_to_list_of_substitutions, perform_substitutions
from rcl_interfaces.srv import GetParameters
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String

LATCHED_QOS = QoSProfile(
    depth=1,
    durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
    reliability=QoSReliabilityPolicy.RELIABLE,
)


@pytest.mark.launch_test
@launch_testing.parametrize("profile", ["brain_client.sim.launch.py", "brain_client.launch.py"])
def generate_test_description(profile):
    # Exercise the actual installed launch arguments, not a separately written
    # Node fixture that could hide a regression in either production profile.
    path = Path(get_package_share_directory("brain_client")) / "launch" / profile
    spec = importlib.util.spec_from_file_location("brain_boot_profile", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    description = module.generate_launch_description()
    context = launch.LaunchContext()
    nodes = {}
    actions = [SetLaunchConfiguration("simulator_mode", "True")]
    for entity in description.entities:
        if isinstance(entity, launch_ros.actions.Node):
            executable = perform_substitutions(context, normalize_to_list_of_substitutions(entity.node_executable))
            if executable not in ("brain_client_node.py", "skills_server.py"):
                continue
            nodes[executable] = entity
        actions.append(entity)
    actions.append(launch_testing.actions.ReadyToTest())
    return launch.LaunchDescription(actions), {
        "brain_client": nodes["brain_client_node.py"],
        "skills_server": nodes["skills_server.py"],
    }


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
        self.assertFalse(status["brain_active"])
        self.assertIn("current_directive", status)

        health = json.loads(self._wait_for_message("/brain/websocket_status", String, timeout_sec=30.0).data)
        # No Gemini credentials in CI: the local brain must say so truthfully
        # rather than crash or claim readiness.
        self.assertIn(health.get("backend"), ("unconfigured", "innate-proxy", "gemini-direct"))
        self.assertIn("connected", health)

        # __node remaps without an original-name prefix apply to every node in
        # the process. The resulting duplicate parameter services can answer
        # with undeclared values even though the main brain is fully configured.
        names = [name for name, namespace in self.node.get_node_names_and_namespaces() if namespace == "/"]
        self.assertEqual(names.count("brain_client_node"), 1)
        self.assertIn("brain_client_service_caller", names)
        client = self.node.create_client(GetParameters, "/brain_client_node/get_parameters")
        try:
            self.assertTrue(client.wait_for_service(timeout_sec=5))
            request = GetParameters.Request()
            request.names = ["brain_provider", "openai_model", "openai_reasoning_effort", "image_topic"]
            for _ in range(5):
                future = client.call_async(request)
                rclpy.spin_until_future_complete(self.node, future, timeout_sec=5)
                self.assertTrue(future.done(), "parameter service did not respond")
                values = future.result().values
                self.assertEqual(len(values), len(request.names))
                self.assertTrue(all(value.type == 4 and value.string_value for value in values))
                self.assertIn(values[0].string_value, ("gemini", "openai"))
                self.assertEqual(values[3].string_value, "/mars/main_camera/left/image_raw/compressed")
        finally:
            self.node.destroy_client(client)
