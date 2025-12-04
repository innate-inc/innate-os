#!/usr/bin/env python3
"""
Launch file for Camera Odom Recorder (standalone).

This launches the camera_odom_recorder node which handles actual recording.
Launch this FIRST, then launch whichever server you need:
- camera_odom_server.launch.py - for manual recording control
- spline_path_server.launch.py - for SLAM trajectory recording

Usage:
    ros2 launch manipulation camera_odom_recorder.launch.py
"""

import os
from launch import LaunchDescription
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    # Locate the package share directory and the YAML configuration file.
    pkg_share = get_package_share_directory('manipulation')
    config_file = os.path.join(pkg_share, 'config', 'camera_odom_recorder.yaml')

    # Define the camera + odom recorder node with its parameters.
    # NOTE: Use consistent node name 'camera_odom_recorder' for service discovery
    camera_odom_recorder_node = Node(
        package='manipulation',
        executable='camera_odom_recorder.py',
        name='camera_odom_recorder',  # Consistent naming for services
        output='screen',
        parameters=[config_file]
    )

    return LaunchDescription([
        camera_odom_recorder_node
    ])

