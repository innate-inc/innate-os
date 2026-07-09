# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Launch file for the innate_console node."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description() -> LaunchDescription:
    # ROS time source: false on the real robot (no /clock); the sim launcher
    # passes true so this node follows the sim driver's /clock.
    use_sim_time_arg = DeclareLaunchArgument("use_sim_time", default_value="false")
    use_sim_time = {"use_sim_time": ParameterValue(LaunchConfiguration("use_sim_time"), value_type=bool)}
    return LaunchDescription(
        [
            use_sim_time_arg,
            Node(
                package="innate_console",
                executable="console_node",
                name="innate_console",
                output="screen",
                parameters=[use_sim_time],
            ),
        ]
    )
