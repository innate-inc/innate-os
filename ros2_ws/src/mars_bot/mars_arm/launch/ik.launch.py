#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    # KDL-based IK node; loads the URDF directly from mars_description (see ik.py).
    return LaunchDescription(
        [
            Node(
                package="mars_arm",
                executable="ik.py",
                name="kdl_ik_from_file",
                output="screen",
            ),
        ]
    )
