#!/usr/bin/env python3
"""
Launch file for the Spline Path Server with integrated Camera/Odom Recorder.

This launches:
1. spline_path_server - WebSocket server for spline path control
2. camera_odom_recorder - Records camera/odom data during trajectory execution

Usage:
    ros2 launch manipulation spline_path_server.launch.py

Then port forward from your PC:
    ssh -L 8770:localhost:8770 user@robot_ip

And open spline_viewer.html in your browser.
"""

import os
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    # Get package share directory
    pkg_share = get_package_share_directory('manipulation')
    
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
    
    # Declare arguments for camera_odom_recorder
    data_directory_arg = DeclareLaunchArgument(
        'data_directory',
        default_value=default_recording_dir,
        description='Directory to save recordings'
    )
    
    data_frequency_arg = DeclareLaunchArgument(
        'data_frequency',
        default_value='10',
        description='Recording frequency in Hz'
    )
    
    camera_topics_arg = DeclareLaunchArgument(
        'camera_topics',
        default_value="['/mars/main_camera/image/compressed']",
        description='Camera topics to record'
    )
    
    head_position_topic_arg = DeclareLaunchArgument(
        'head_position_topic',
        default_value='/mars/head/current_position',
        description='Head position topic (empty to disable)'
    )
    
    # Spline Path Server node
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
    
    # Camera Odom Recorder node
    camera_odom_recorder_node = Node(
        package='manipulation',
        executable='camera_odom_recorder.py',
        name='camera_odom_recorder',
        output='screen',
        parameters=[{
            'data_directory': LaunchConfiguration('data_directory'),
            'data_frequency': LaunchConfiguration('data_frequency'),
            'camera_topics': LaunchConfiguration('camera_topics'),
            'odom_topic': LaunchConfiguration('odom_topic'),
            'head_position_topic': LaunchConfiguration('head_position_topic'),
            'session_name_prefix': 'spline_traj',
            'chunk_size': 100,
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
        data_frequency_arg,
        camera_topics_arg,
        head_position_topic_arg,
        # Nodes
        camera_odom_recorder_node,
        spline_path_server_node,
    ])
