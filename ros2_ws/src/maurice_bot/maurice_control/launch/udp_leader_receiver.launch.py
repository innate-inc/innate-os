#!/usr/bin/env python3
"""
Launch file for UDP Leader Receiver node
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    # Declare launch arguments
    port_arg = DeclareLaunchArgument(
        'port',
        default_value='9999',
        description='UDP port to listen on'
    )
    
    buffer_size_arg = DeclareLaunchArgument(
        'buffer_size',
        default_value='1024',
        description='UDP socket buffer size in bytes'
    )
    
    auto_start_arg = DeclareLaunchArgument(
        'auto_start',
        default_value='true',
        description='Automatically start UDP receiver on launch'
    )
    
    log_rate_arg = DeclareLaunchArgument(
        'log_rate',
        default_value='10.0',
        description='Rate (in seconds) at which to log statistics'
    )
    
    # Create the node (C++ implementation for low-latency)
    udp_leader_receiver_node = Node(
        package='maurice_control',
        executable='udp_leader_receiver',
        name='udp_leader_receiver',
        output='screen',
        parameters=[{
            'port': LaunchConfiguration('port'),
            'buffer_size': LaunchConfiguration('buffer_size'),
            'auto_start': LaunchConfiguration('auto_start'),
            'log_rate': LaunchConfiguration('log_rate'),
        }]
    )
    
    return LaunchDescription([
        port_arg,
        buffer_size_arg,
        auto_start_arg,
        log_rate_arg,
        udp_leader_receiver_node,
    ])

