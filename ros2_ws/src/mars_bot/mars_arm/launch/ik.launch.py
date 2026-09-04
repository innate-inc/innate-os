#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    # KDL-based IK node; loads the URDF directly from mars_sim (see ik.py).
    return LaunchDescription(
        [
            Node(
                package="mars_arm",
                executable="ik.py",
                name="kdl_ik_from_file",
                # Teleop streams through this node at 30 Hz; a crash must not
                # leave the arm without IK until the next restart.
                respawn=True,
                respawn_delay=2.0,
                output="screen",
            ),
        ]
    )
