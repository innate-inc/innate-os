#!/usr/bin/env python3

import os
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    # Get package directories
    manipulation_share = FindPackageShare('manipulation')
    
    # Declare launch arguments
    config_file_arg = DeclareLaunchArgument(
        'config_file',
        default_value=PathJoinSubstitution([
            manipulation_share,
            'config',
            'behaviors.yaml'
        ]),
        description='Path to the behaviors configuration file'
    )
    
    recorder_config_arg = DeclareLaunchArgument(
        'recorder_config',
        default_value=PathJoinSubstitution([
            manipulation_share,
            'config',
            'recorder.yaml'
        ]),
        description='Path to the recorder configuration file'
    )
    
    log_level_arg = DeclareLaunchArgument(
        'log_level',
        default_value='info',
        description='Log level for the behavior server'
    )
    
    # Behavior server node
    behavior_server_node = Node(
        package='manipulation',
        executable='behavior_server.py',
        name='behavior_server',
        output='screen',
        parameters=[
            {'config_file': LaunchConfiguration('config_file')},
            {'recorder_config': LaunchConfiguration('recorder_config')},
            LaunchConfiguration('recorder_config')  # This loads the entire YAML file as parameters
        ],
        arguments=['--ros-args', '--log-level', LaunchConfiguration('log_level')],
        emulate_tty=True,
        respawn=True,
        respawn_delay=2.0
    )
    
    return LaunchDescription([
        config_file_arg,
        recorder_config_arg,
        log_level_arg,
        behavior_server_node
    ])
