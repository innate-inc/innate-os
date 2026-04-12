#!/usr/bin/env python3

from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    # Node to launch the RPLidar driver
    lidar_node = Node(
        package='rplidar_ros',
        executable='rplidar_node',
        name='rplidar_node',
        parameters=[{
            'channel_type': 'serial',
            'serial_port': '/dev/rplidar',
            'serial_baudrate': 115200,
            'frame_id': 'base_laser',
            'inverted': False,
            'angle_compensate': True,
            'scan_mode': 'Express',
        }],
        output='screen',
        remappings=[('scan', 'scan_fast')]
    )

    # base_link -> base_laser static TF is now published by
    # robot_state_publisher via the URDF (base_laser_joint).

    # Node to throttle scan_fast into scan
    throttle_node = Node(
        package='topic_tools',
        executable='throttle',
        name='scan_throttle',
        arguments=['messages', '/scan_fast', '6.0', '/scan'],
        output='screen'
    )

    return LaunchDescription([
        lidar_node,
        throttle_node,
    ])