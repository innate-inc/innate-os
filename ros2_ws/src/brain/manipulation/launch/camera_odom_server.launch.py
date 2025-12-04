#!/usr/bin/env python3
"""
Launch file for Camera Odom Server (WebSocket interface only).

This launches ONLY the camera_odom_server WebSocket node.
The camera_odom_recorder must be launched separately!

Usage:
    # First, launch the recorder (if not already running):
    ros2 launch manipulation camera_odom_recorder.launch.py
    
    # Then launch this server:
    ros2 launch manipulation camera_odom_server.launch.py

Then port forward from your PC:
    ssh -L 8771:localhost:8771 user@robot_ip

And connect via the unified pipeline viewer.

NOTE: Do NOT run this alongside spline_path_server.launch.py with both
launching the recorder - only ONE recorder should be running at a time.
"""

import os
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    # Default recording directory
    maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
    default_recording_dir = os.path.join(maurice_root, 'camera_odom_recordings')
    
    # Declare arguments
    websocket_port_arg = DeclareLaunchArgument(
        'websocket_port',
        default_value='8771',
        description='WebSocket server port for camera_odom_server'
    )
    
    data_directory_arg = DeclareLaunchArgument(
        'data_directory',
        default_value=default_recording_dir,
        description='Directory where recordings are saved (must match recorder)'
    )
    
    # Camera Odom Server node (WebSocket interface ONLY)
    # NOTE: This assumes camera_odom_recorder is already running!
    camera_odom_server_node = Node(
        package='manipulation',
        executable='camera_odom_server.py',
        name='camera_odom_server',
        output='screen',
        parameters=[{
            'websocket_port': LaunchConfiguration('websocket_port'),
            'recording_data_dir': LaunchConfiguration('data_directory'),
        }]
    )
    
    return LaunchDescription([
        # Arguments
        websocket_port_arg,
        data_directory_arg,
        # Nodes - ONLY the server, NOT the recorder
        camera_odom_server_node,
    ])
