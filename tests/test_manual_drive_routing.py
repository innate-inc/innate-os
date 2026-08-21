# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Host-safe contracts for manual base-command routing."""

import ast
import sys
from pathlib import Path
from types import MethodType, ModuleType, SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "ros2_ws/src/mars_bot/mars_control"))
sys.path.insert(0, str(_REPO_ROOT / "ros2_ws/src/mars_bot/mars_nav"))
sys.path.insert(0, str(_REPO_ROOT / "ros2_ws/src/brain/brain_client"))
sys.path.insert(0, str(_REPO_ROOT / "ros2_ws/src/brain/brain_client/test"))

from ros_skill_test_stubs import install_if_ros_is_missing  # noqa: E402

install_if_ros_is_missing()

# The shared host ROS shim predates cmd_vel_mux's authoritative SetBool safety
# service. Keep this test importable in either order without changing the brain
# test-support package from this mars_nav-focused change.
try:
    from std_srvs.srv import SetBool as _SetBool  # noqa: F401
except (ImportError, ModuleNotFoundError):
    std_srvs_module = sys.modules.setdefault("std_srvs", ModuleType("std_srvs"))
    std_srvs_module.__path__ = []
    std_srvs_srv_module = ModuleType("std_srvs.srv")

    class _SetBool:
        class Request:
            def __init__(self):
                self.data = False

        class Response:
            def __init__(self):
                self.success = False
                self.message = ""

    class _Trigger:
        class Request:
            pass

        class Response:
            def __init__(self):
                self.success = False
                self.message = ""

    std_srvs_srv_module.SetBool = _SetBool
    std_srvs_srv_module.Trigger = _Trigger
    std_srvs_module.srv = std_srvs_srv_module
    sys.modules["std_srvs.srv"] = std_srvs_srv_module

from geometry_msgs.msg import Twist  # noqa: E402

from mars_control.teleop_publish_gate import TeleopPublishGate  # noqa: E402
from mars_nav.cmd_vel_mux import SOURCES, CmdVelMux  # noqa: E402


@pytest.mark.parametrize(
    "relative_path",
    [
        "ros2_ws/src/mars_bot/mars_control/mars_control/joystick.py",
        "ros2_ws/src/mars_bot/mars_control/mars_control/keyboard.py",
    ],
)
def test_manual_controller_twist_publishers_use_teleop_mux_input(relative_path):
    tree = ast.parse((_REPO_ROOT / relative_path).read_text())
    topics = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "create_publisher" or len(node.args) < 2:
            continue
        message_type, topic = node.args[:2]
        if isinstance(message_type, ast.Name) and message_type.id == "Twist":
            assert isinstance(topic, ast.Constant) and isinstance(topic.value, str)
            topics.append(topic.value)

    assert topics == ["/cmd_vel_teleop"]
    assert "/cmd_vel" not in topics


@pytest.mark.parametrize(
    "launch_name",
    ["brain_client.launch.py", "brain_client.sim.launch.py"],
)
def test_brain_and_code_skills_use_the_skills_priority_mux_input(launch_name):
    source = (_REPO_ROOT / "ros2_ws/src/brain/brain_client/launch" / launch_name).read_text()

    assert 'default_value="/cmd_vel_skills"' in source
    # The one launch argument must feed both brain_client_node and the
    # skills_action_server. A simulator-only direct /cmd_vel writer would
    # bypass the mapping Slow envelope and priority arbitration.
    assert source.count('"cmd_vel_topic": LaunchConfiguration("cmd_vel_topic")') == 2


def test_brain_motion_defaults_and_gaze_cannot_bypass_the_mux():
    brain_root = _REPO_ROOT / "ros2_ws/src/brain/brain_client/brain_client"
    config_source = (brain_root / "core/config.py").read_text()
    server_source = (brain_root / "nodes/skills_server.py").read_text()
    mobility_source = (brain_root / "robot/mobility.py").read_text()
    gaze_source = (brain_root / "perception/gaze.py").read_text()
    camera_source = (brain_root / "perception/camera.py").read_text()

    assert '"cmd_vel_topic": "/cmd_vel_skills"' in config_source
    assert '"motion_observation_topic": "/cmd_vel"' in config_source
    assert 'declare_parameter("cmd_vel_topic", "/cmd_vel_skills")' in server_source
    assert 'cmd_vel_topic: str = "/cmd_vel_skills"' in mobility_source
    assert 'get_parameter("cmd_vel_topic")' in gaze_source
    assert 'Mobility(node, node.get_logger(), "/cmd_vel")' not in gaze_source
    # This is deliberately not the publish topic: camera motion suppression
    # must also see manual teleop and Nav2 at the final base output.
    assert "_config.motion_observation_topic" in camera_source
    assert "_config.cmd_vel_topic, self._on_cmd_vel" not in camera_source


def test_sim_vision_navigation_enters_the_nav_mux_path():
    script = (_REPO_ROOT / "scripts/launch_sim_in_tmux.zsh").read_text()
    protocol = (_REPO_ROOT / "ros2_ws/src/cloud/innate_uninavid/PROTOCOL.md").read_text()

    assert "innate_uninavid uninavid.launch.py cmd_vel_topic:=/cmd_vel_scaled" in script
    assert 'innate_uninavid uninavid.launch.py cmd_vel_topic:=/cmd_vel"' not in script
    assert "cmd_vel_topic:=/cmd_vel" not in protocol


def test_teleop_source_streams_motion_then_publishes_one_settling_zero():
    gate = TeleopPublishGate()

    assert gate.next_command(0.0, 0.0) is None
    assert gate.next_command(0.2, -0.1) == (0.2, -0.1)
    assert gate.next_command(0.1, 0.0) == (0.1, 0.0)
    assert gate.next_command(0.0, 0.0) == (0.0, 0.0)
    assert gate.next_command(0.0, 0.0) is None


def test_disabling_an_active_teleop_source_releases_the_mux_once():
    gate = TeleopPublishGate()

    assert gate.next_command(0.2, 0.0) == (0.2, 0.0)
    assert gate.next_command(0.2, 0.0, enabled=False) == (0.0, 0.0)
    assert gate.next_command(0.2, 0.0, enabled=False) is None


@pytest.mark.parametrize(("priority", "source"), list(enumerate(name for name, _topic, _window in SOURCES)))
def test_every_mux_source_is_capped_at_the_slow_envelope_while_mapping(priority, source):
    published = []
    mux = SimpleNamespace(
        _mapping_limit_enabled=True,
        _mapping_max_linear=0.14,
        _mapping_max_angular=0.21,
        _last_rx={name: 0.0 for name, _topic, _window in SOURCES},
        _fresh_source_above=lambda _priority, _now: None,
        _pub=SimpleNamespace(publish=published.append),
        _forwarding=False,
    )
    mux._apply_mapping_speed_limit = MethodType(CmdVelMux._apply_mapping_speed_limit, mux)
    command = Twist()
    command.linear.x = -0.4
    command.linear.y = 100.0
    command.linear.z = -100.0
    command.angular.x = 100.0
    command.angular.y = -100.0
    command.angular.z = 0.6

    CmdVelMux._on_twist(mux, priority, source, command)

    assert len(published) == 1
    assert published[0].linear.x == pytest.approx(-0.14)
    assert published[0].angular.z == pytest.approx(0.21)
    assert (published[0].linear.y, published[0].linear.z) == (0.0, 0.0)
    assert (published[0].angular.x, published[0].angular.y) == (0.0, 0.0)
    assert mux._forwarding is True


def test_mux_returns_the_original_command_only_when_the_envelope_is_disabled():
    mux = SimpleNamespace(_mapping_limit_enabled=False, _mapping_max_linear=0.14, _mapping_max_angular=0.21)
    command = Twist()
    command.linear.x = 0.4
    command.angular.z = 1.0

    assert CmdVelMux._apply_mapping_speed_limit(mux, command) is command


@pytest.mark.parametrize(("linear", "angular"), [(float("nan"), 0.0), (0.0, float("inf"))])
def test_mux_replaces_non_finite_mapping_commands_with_a_finite_zero(linear, angular):
    mux = SimpleNamespace(_mapping_limit_enabled=True, _mapping_max_linear=0.14, _mapping_max_angular=0.21)
    command = Twist()
    command.linear.x = linear
    command.angular.z = angular

    limited = CmdVelMux._apply_mapping_speed_limit(mux, command)

    assert (limited.linear.x, limited.linear.y, limited.linear.z) == (0.0, 0.0, 0.0)
    assert (limited.angular.x, limited.angular.y, limited.angular.z) == (0.0, 0.0, 0.0)


def test_enabling_the_authoritative_envelope_publishes_zero_before_success_ack():
    response = SimpleNamespace(success=False, message="")
    events = []

    def publish(msg):
        assert response.success is False
        events.append((msg.linear.x, msg.angular.z))

    mux = SimpleNamespace(
        _mapping_limit_enabled=False,
        _has_authoritative_limit_source=False,
        _forwarding=True,
        _pub=SimpleNamespace(publish=publish),
    )
    mux._publish_zero_stop = MethodType(CmdVelMux._publish_zero_stop, mux)

    returned = CmdVelMux._set_mapping_speed_limit(mux, SimpleNamespace(data=True), response)

    assert returned is response
    assert events == [(0.0, 0.0)]
    assert response.success is True
    assert mux._mapping_limit_enabled is True
    assert mux._has_authoritative_limit_source is True
    assert mux._forwarding is False


def test_queued_mode_samples_are_fallback_only_after_an_authoritative_request():
    published = []
    mux = SimpleNamespace(
        _mapping_limit_enabled=True,
        _has_authoritative_limit_source=False,
        _forwarding=False,
        _pub=SimpleNamespace(publish=published.append),
        get_logger=lambda: SimpleNamespace(debug=lambda _message: None),
    )
    mux._publish_zero_stop = MethodType(CmdVelMux._publish_zero_stop, mux)

    CmdVelMux._set_mapping_speed_limit(
        mux,
        SimpleNamespace(data=False),
        SimpleNamespace(success=False, message=""),
    )
    CmdVelMux._on_mode(mux, SimpleNamespace(data="mapping"))
    assert mux._mapping_limit_enabled is False

    CmdVelMux._set_mapping_speed_limit(
        mux,
        SimpleNamespace(data=True),
        SimpleNamespace(success=False, message=""),
    )
    CmdVelMux._on_mode(mux, SimpleNamespace(data="navigation"))
    assert mux._mapping_limit_enabled is True
    assert len(published) == 1


def test_mode_topic_fallback_enables_slow_and_zeros_motion_before_authority_exists():
    published = []
    mux = SimpleNamespace(
        _mapping_limit_enabled=False,
        _has_authoritative_limit_source=False,
        _forwarding=True,
        _pub=SimpleNamespace(publish=published.append),
    )
    mux._publish_zero_stop = MethodType(CmdVelMux._publish_zero_stop, mux)

    CmdVelMux._on_mode(mux, SimpleNamespace(data="switching"))

    assert mux._mapping_limit_enabled is True
    assert mux._forwarding is False
    assert len(published) == 1
    assert published[0].linear.x == 0.0
    assert published[0].angular.z == 0.0


def test_mux_subscribes_to_the_latched_navigation_mode_and_loads_motion_overrides():
    mux_source = (_REPO_ROOT / "ros2_ws/src/mars_bot/mars_nav/mars_nav/cmd_vel_mux.py").read_text()
    launch_source = (_REPO_ROOT / "ros2_ws/src/mars_bot/mars_nav/launch/mode_manager.launch.py").read_text()

    assert '"/nav/current_mode"' in mux_source
    assert "QoSDurabilityPolicy.TRANSIENT_LOCAL" in mux_source
    assert "QoSReliabilityPolicy.RELIABLE" in mux_source
    assert 'declare_parameter("motion_control.max_speed"' in mux_source
    assert 'declare_parameter("motion_control.max_angular_speed"' in mux_source
    assert 'declare_parameter("nav.max_speed"' in mux_source
    assert 'declare_parameter("nav.max_angular_speed"' in mux_source
    assert "MAPPING_SPEED_LIMIT_SERVICE" in mux_source
    assert "create_service(" in mux_source
    assert "parameters=[*settings_params()]" in launch_source


def test_mars_app_reports_slow_for_both_mapping_modes_and_holds_it_through_switching():
    app_source = (_REPO_ROOT / "ros2_ws/src/mars_bot/mars_control/mars_control/app.cpp").read_text()
    speed_modes = (_REPO_ROOT / "ros2_ws/src/mars_bot/mars_control/mars_control/drive_smoother.hpp").read_text()

    assert '"/nav/current_mode", rclcpp::QoS(1).reliable().transient_local()' in app_source
    assert 'msg->data == "mapping" || msg->data == "autonomous_mapping"' in app_source
    assert 'if (msg->data == "switching")' in app_source
    assert 'MAPPING_SCALE_ID = "slow"' in speed_modes
    assert "mapping_ && std::abs(value - mapping_scale) > 1e-9" in app_source
    assert "Mapping locks motion_control.speed_scale to the Slow preset" in app_source
