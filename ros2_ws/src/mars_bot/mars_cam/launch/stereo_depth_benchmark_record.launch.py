#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc

from datetime import datetime
from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess
from launch.substitutions import LaunchConfiguration


def generate_launch_description() -> LaunchDescription:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    default_output = str(Path.home() / "innate-os" / "recordings" / f"stereo_canonical_{ts}")

    output_bag = DeclareLaunchArgument(
        "output_bag",
        default_value=default_output,
        description="Canonical bag output directory",
    )

    lidar_topic = DeclareLaunchArgument(
        "lidar_topic",
        default_value="/lidar",
        description="Reference lidar topic (/lidar or /scan)",
    )

    record = ExecuteProcess(
        cmd=[
            "ros2",
            "bag",
            "record",
            "/left/image_raw",
            "/right/image_raw",
            "/left/camera_info",
            "/right/camera_info",
            "/tf",
            "/tf_static",
            LaunchConfiguration("lidar_topic"),
            "/odom",
            "/imu",
            "-o",
            LaunchConfiguration("output_bag"),
        ],
        output="screen",
    )

    return LaunchDescription([output_bag, lidar_topic, record])
