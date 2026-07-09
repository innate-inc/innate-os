#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc


from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from launch_ros.substitutions import FindPackageShare
from mars_bringup.config_loader import settings_params, workspace_skills_dir


def generate_launch_description():
    # Get package directories
    manipulation_share = FindPackageShare("manipulation")

    # Declare launch arguments
    manipulation_config_arg = DeclareLaunchArgument(
        "manipulation_config",
        default_value=PathJoinSubstitution([manipulation_share, "config", "manipulation_server.yaml"]),
        description="Path to the manipulation server configuration file",
    )

    log_level_arg = DeclareLaunchArgument(
        "log_level", default_value="info", description="Log level for the behavior server"
    )

    # ROS time source: false on the real robot (no /clock); the sim launcher
    # passes true so this node follows the sim driver's /clock.
    use_sim_time_arg = DeclareLaunchArgument("use_sim_time", default_value="false")
    use_sim_time = {"use_sim_time": ParameterValue(LaunchConfiguration("use_sim_time"), value_type=bool)}

    # Resolved here because ROS YAML can't expand $INNATE_OS_ROOT. Mirrors recorder.launch.py.
    data_directory = str(workspace_skills_dir())

    # Behavior server node
    behavior_server_node = Node(
        package="manipulation",
        executable="manipulation_server.py",
        name="manipulation_server",
        output="screen",
        parameters=[
            LaunchConfiguration("manipulation_config"),
            {"data_directory": data_directory},  # env-resolved; beats the YAML fallback
            use_sim_time,
            *settings_params(),  # settings.yaml overrides, layered last
        ],
        arguments=["--ros-args", "--log-level", LaunchConfiguration("log_level")],
        emulate_tty=True,
        respawn=True,
        respawn_delay=2.0,
    )

    return LaunchDescription([manipulation_config_arg, log_level_arg, use_sim_time_arg, behavior_server_node])
