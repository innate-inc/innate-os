#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
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
    simulator_mode_arg = DeclareLaunchArgument(
        "simulator_mode",
        default_value="true" if os.environ.get("VIRTUAL_MARS_REMOTE") else "false",
        description="Route replay audio cues through the simulator browser",
    )

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
            {
                "data_directory": data_directory,
                "simulator_mode": LaunchConfiguration("simulator_mode"),
            },
            *settings_params(),  # settings.yaml overrides, layered last
        ],
        arguments=["--ros-args", "--log-level", LaunchConfiguration("log_level")],
        emulate_tty=True,
        respawn=True,
        respawn_delay=2.0,
    )

    return LaunchDescription([manipulation_config_arg, log_level_arg, simulator_mode_arg, behavior_server_node])
