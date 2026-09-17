#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    use_sim_time = DeclareLaunchArgument("use_sim_time", default_value="true")
    model_package = DeclareLaunchArgument("model_package", default_value="light_stereo")
    model_executable = DeclareLaunchArgument("model_executable", default_value="light_stereo_node")
    left_image_topic = DeclareLaunchArgument("left_image_topic", default_value="/left/image_raw")
    right_image_topic = DeclareLaunchArgument("right_image_topic", default_value="/right/image_raw")
    left_camera_info_topic = DeclareLaunchArgument("left_camera_info_topic", default_value="/left/camera_info")
    right_camera_info_topic = DeclareLaunchArgument("right_camera_info_topic", default_value="/right/camera_info")
    depth_topic = DeclareLaunchArgument("depth_topic", default_value="/stereo/light/depth")
    model_left_image_topic = DeclareLaunchArgument("model_left_image_topic", default_value="left/image_raw")
    model_right_image_topic = DeclareLaunchArgument("model_right_image_topic", default_value="right/image_raw")
    model_left_camera_info_topic = DeclareLaunchArgument("model_left_camera_info_topic", default_value="left/camera_info")
    model_right_camera_info_topic = DeclareLaunchArgument("model_right_camera_info_topic", default_value="right/camera_info")
    model_depth_topic = DeclareLaunchArgument("model_depth_topic", default_value="depth/image_rect_raw")
    log_level = DeclareLaunchArgument("log_level", default_value="warn")

    node = Node(
        package=LaunchConfiguration("model_package"),
        executable=LaunchConfiguration("model_executable"),
        name="light_stereo",
        output="screen",
        parameters=[{"use_sim_time": LaunchConfiguration("use_sim_time")}],
        remappings=[
            (LaunchConfiguration("model_left_image_topic"), LaunchConfiguration("left_image_topic")),
            (LaunchConfiguration("model_right_image_topic"), LaunchConfiguration("right_image_topic")),
            (LaunchConfiguration("model_left_camera_info_topic"), LaunchConfiguration("left_camera_info_topic")),
            (LaunchConfiguration("model_right_camera_info_topic"), LaunchConfiguration("right_camera_info_topic")),
            (LaunchConfiguration("model_depth_topic"), LaunchConfiguration("depth_topic")),
        ],
        arguments=["--ros-args", "--log-level", LaunchConfiguration("log_level")],
    )

    return LaunchDescription(
        [
            use_sim_time,
            model_package,
            model_executable,
            left_image_topic,
            right_image_topic,
            left_camera_info_topic,
            right_camera_info_topic,
            depth_topic,
            model_left_image_topic,
            model_right_image_topic,
            model_left_camera_info_topic,
            model_right_camera_info_topic,
            model_depth_topic,
            log_level,
            node,
        ]
    )
