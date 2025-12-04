#!/usr/bin/env python3
"""
Launch file for the Spline Path Server (WebSocket interface only).

This launches ONLY the spline_path_server WebSocket node.
The camera_odom_recorder must be launched separately!

Usage:
    # First, launch the recorder (if not already running):
    ros2 launch manipulation camera_odom_recorder.launch.py
    
    # Then launch this server:
    ros2 launch manipulation spline_path_server.launch.py

Then port forward from your PC:
    ssh -L 8770:localhost:8770 user@robot_ip

And open spline_viewer.html in your browser.

NOTE: Do NOT run this alongside camera_odom_server.launch.py with both
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
    
    # Declare arguments for spline_path_server
    websocket_port_arg = DeclareLaunchArgument(
        'websocket_port',
        default_value='8770',
        description='WebSocket server port'
    )
    
    use_amcl_pose_arg = DeclareLaunchArgument(
        'use_amcl_pose',
        default_value='true',
        description='Use AMCL pose (true) or odometry (false)'
    )
    
    map_topic_arg = DeclareLaunchArgument(
        'map_topic',
        default_value='/map',
        description='Map topic to subscribe to'
    )
    
    odom_topic_arg = DeclareLaunchArgument(
        'odom_topic',
        default_value='/odom',
        description='Odometry topic'
    )
    
    amcl_pose_topic_arg = DeclareLaunchArgument(
        'amcl_pose_topic',
        default_value='/amcl_pose',
        description='AMCL pose topic'
    )
    
    data_directory_arg = DeclareLaunchArgument(
        'data_directory',
        default_value=default_recording_dir,
        description='Directory where recordings are saved (must match recorder)'
    )
    
    # Spline Path Server node (WebSocket interface ONLY)
    # NOTE: This assumes camera_odom_recorder is already running!
    spline_path_server_node = Node(
        package='manipulation',
        executable='spline_path_server.py',
        name='spline_path_server',
        output='screen',
        parameters=[{
            'websocket_port': LaunchConfiguration('websocket_port'),
            'use_amcl_pose': LaunchConfiguration('use_amcl_pose'),
            'map_topic': LaunchConfiguration('map_topic'),
            'odom_topic': LaunchConfiguration('odom_topic'),
            'amcl_pose_topic': LaunchConfiguration('amcl_pose_topic'),
            'recording_data_dir': LaunchConfiguration('data_directory'),
        }]
    )
    
    return LaunchDescription([
        # Arguments
        websocket_port_arg,
        use_amcl_pose_arg,
        map_topic_arg,
        odom_topic_arg,
        amcl_pose_topic_arg,
        data_directory_arg,
        # Nodes - ONLY the server, NOT the recorder
        spline_path_server_node,
    ])
