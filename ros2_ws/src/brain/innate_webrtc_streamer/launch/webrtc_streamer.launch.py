#!/usr/bin/env python3
"""
Launch file for WebRTC Streamer node.

This launches the WebRTC video streaming node that provides low-latency
H.264 video over WebRTC to connected clients (e.g., React Native app).
"""

import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    # Get the package directory
    pkg_dir = get_package_share_directory('innate_webrtc_streamer')
    
    # Path to config file
    config_file = os.path.join(pkg_dir, 'config', 'webrtc_params.yaml')
    
    # Create the WebRTC streamer node
    webrtc_streamer_node = Node(
        package='innate_webrtc_streamer',
        executable='webrtc_streamer_node.py',
        name='webrtc_streamer_node',
        output='screen',
        parameters=[config_file],
        emulate_tty=True,
    )
    
    return LaunchDescription([
        webrtc_streamer_node
    ])


