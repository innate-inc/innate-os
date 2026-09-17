"""lerobot finds the plugin by its package name and builds the devices from their configs."""

from __future__ import annotations

from lerobot.robots.config import RobotConfig
from lerobot.robots.utils import make_robot_from_config
from lerobot.teleoperators.config import TeleoperatorConfig
from lerobot.teleoperators.utils import make_teleoperator_from_config
from lerobot.utils.import_utils import register_third_party_plugins

from lerobot_robot_mars import Mars, MarsConfig, MarsPassthroughTeleop, MarsPassthroughTeleopConfig


def test_plugin_registers_its_types() -> None:
    register_third_party_plugins()
    assert RobotConfig.get_known_choices()["mars"] is MarsConfig
    assert TeleoperatorConfig.get_known_choices()["mars_passthrough"] is MarsPassthroughTeleopConfig


def test_factories_build_the_devices_without_connecting() -> None:
    robot = make_robot_from_config(MarsConfig(remote_ip="127.0.0.1"))
    assert isinstance(robot, Mars) and not robot.is_connected
    assert list(robot.observation_features) == [*robot.action_features][:6] + ["head", "wrist"]
    teleop = make_teleoperator_from_config(MarsPassthroughTeleopConfig(remote_ip="127.0.0.1"))
    assert isinstance(teleop, MarsPassthroughTeleop) and not teleop.is_connected
    assert teleop.action_features == robot.action_features
