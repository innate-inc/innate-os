#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description() -> LaunchDescription:
    use_sim_time = DeclareLaunchArgument("use_sim_time", default_value="true")
    camera_config = DeclareLaunchArgument(
        "camera_config",
        default_value=PathJoinSubstitution([FindPackageShare("mars_cam"), "config", "stereo_depth_estimator.yaml"]),
        description="Stereo depth estimator config file",
    )
    log_level = DeclareLaunchArgument("log_level", default_value="warn")

    estimator = Node(
        package="mars_cam",
        executable="stereo_depth_estimator",
        name="stereo_depth_estimator",
        output="screen",
        parameters=[
            LaunchConfiguration("camera_config"),
            {"use_sim_time": LaunchConfiguration("use_sim_time")},
        ],
        arguments=["--ros-args", "--log-level", LaunchConfiguration("log_level")],
    )

    return LaunchDescription([use_sim_time, camera_config, log_level, estimator])
