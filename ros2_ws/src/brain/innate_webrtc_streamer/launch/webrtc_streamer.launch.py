#!/usr/bin/env python3
"""Simple launch file for WebRTC video streamer."""

from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        Node(
            package='innate_webrtc_streamer',
            executable='webrtc_streamer.py',
            name='webrtc_streamer',
            output='screen',
        )
    ])
