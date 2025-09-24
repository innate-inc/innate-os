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
            'scan_mode': 'Standard',
        }],
        output='screen',
        remappings=[('scan', 'scan_fast')]
    )

    # Node to publish a static transform from base_link to laser_frame
    static_tf_node = Node(
        package='tf2_ros',
        executable='static_transform_publisher',
        name='lidar_tf',
        arguments=[
            "-0.0764", "0.0", "0.17165",  # Translation: X, Y, Z (in meters)
            "0", "0", "0",                # Rotation: roll, pitch, yaw (in radians)
            "base_link", "base_laser"      # Parent and child frames
        ]
    )

    # Node to throttle scan_fast into scan
    throttle_node = Node(
        package='topic_tools',
        executable='throttle',
        name='scan_throttle',
        arguments=['messages', '/scan_fast', '3.0', '/scan'],
        output='screen'
    )

    return LaunchDescription([
        lidar_node,
        static_tf_node,
        throttle_node,
    ])