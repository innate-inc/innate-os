#!/usr/bin/env python3
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        # KDL-based IK node
        Node(
            package='maurice_arm',
            executable='ik.py',
            name='kdl_ik_from_file',
            output='screen',
        ),
    ])
