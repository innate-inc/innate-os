# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    # Launch configurations for sim time.
    use_sim_time = LaunchConfiguration("use_sim_time")

    # Get the parameter file from the package share directory.
    slam_params_file = os.path.join(get_package_share_directory("mars_nav"), "config", "mapping_params_init.yaml")

    # Declare launch arguments.
    declare_use_sim_time_argument = DeclareLaunchArgument(
        "use_sim_time",
        default_value="false",
        description="Use simulation clock (the native simulator and hardware both use wall time by default)",
    )

    # Configure the asynchronous slam_toolbox node using the parameter file.
    async_slam_toolbox_node = Node(
        parameters=[slam_params_file, {"use_lifecycle_manager": True, "use_sim_time": use_sim_time}],
        package="slam_toolbox",
        executable="async_slam_toolbox_node",
        name="slam_toolbox",
        namespace="",
        output="screen",
        # slam_toolbox boot chatter at WARN to keep `innate view` readable.
        arguments=["--ros-args", "--log-level", "warn"],
    )
    ld = LaunchDescription()

    ld.add_action(declare_use_sim_time_argument)
    ld.add_action(async_slam_toolbox_node)
    return ld
