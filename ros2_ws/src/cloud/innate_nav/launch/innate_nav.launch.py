# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Launch file for the innate_nav node."""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    pkg_dir = get_package_share_directory("innate_nav")
    default_params = os.path.join(pkg_dir, "config", "params.yaml")

    params_arg = DeclareLaunchArgument(
        "params_file",
        default_value=default_params,
        description="Full path to the parameter YAML file",
    )
    cmd_vel_topic_arg = DeclareLaunchArgument(
        "cmd_vel_topic",
        default_value="/cmd_vel_nav",
        description="Velocity topic the policy commands should publish to",
    )

    # Sim renders the policy's view natively, so this is hardware-only.
    wide_view_arg = DeclareLaunchArgument(
        "wide_view",
        default_value="false",
        description="Remap the fisheye into the policy's pinhole view (hardware; sim renders it)",
    )
    wide_view = Node(
        package="innate_nav",
        executable="wide_view",
        name="wide_view",
        output="screen",
        parameters=[LaunchConfiguration("params_file")],
        condition=IfCondition(LaunchConfiguration("wide_view")),
    )

    node = Node(
        package="innate_nav",
        executable="innate_nav_node",
        name="innate_nav_node",
        output="screen",
        parameters=[LaunchConfiguration("params_file")],
        remappings=[
            ("/cmd_vel_nav", LaunchConfiguration("cmd_vel_topic")),
        ],
    )

    return LaunchDescription([params_arg, cmd_vel_topic_arg, wide_view_arg, wide_view, node])
