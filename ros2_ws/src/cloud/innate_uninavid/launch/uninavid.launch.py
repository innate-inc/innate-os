# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Launch file for the innate_uninavid node."""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
from mars_bringup.config_loader import settings_params


def generate_launch_description() -> LaunchDescription:
    pkg_dir = get_package_share_directory("innate_uninavid")
    default_params = os.path.join(pkg_dir, "config", "params.yaml")

    params_arg = DeclareLaunchArgument(
        "params_file",
        default_value=default_params,
        description="Full path to the parameter YAML file",
    )
    cmd_vel_topic_arg = DeclareLaunchArgument(
        "cmd_vel_topic",
        default_value="/cmd_vel_scaled",
        description="Velocity topic UniNavid commands should publish to",
    )

    # ROS time source: false on the real robot (no /clock); the sim launcher
    # passes true so this node follows the sim driver's /clock.
    use_sim_time_arg = DeclareLaunchArgument("use_sim_time", default_value="false")
    use_sim_time = {"use_sim_time": ParameterValue(LaunchConfiguration("use_sim_time"), value_type=bool)}

    node = Node(
        package="innate_uninavid",
        executable="uninavid_node",
        name="uninavid_node",
        output="screen",
        parameters=[LaunchConfiguration("params_file"), use_sim_time, *settings_params()],
        remappings=[
            ("/cmd_vel", LaunchConfiguration("cmd_vel_topic")),
        ],
    )

    return LaunchDescription([params_arg, cmd_vel_topic_arg, use_sim_time_arg, node])
