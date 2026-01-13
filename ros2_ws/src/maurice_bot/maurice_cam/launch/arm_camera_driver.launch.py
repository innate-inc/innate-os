#!/usr/bin/env python3

import os
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    # Declare launch arguments
    camera_symlink_arg = DeclareLaunchArgument(
        'camera_symlink',
        default_value='Arducam',
        description='Camera symlink pattern (searches for symlinks containing this pattern in /dev/v4l/by-id/)'
    )
    
    width_arg = DeclareLaunchArgument(
        'width',
        default_value='640',
        description='Camera capture width'
    )
    
    height_arg = DeclareLaunchArgument(
        'height',
        default_value='480',
        description='Camera capture height'
    )
    
    fps_arg = DeclareLaunchArgument(
        'fps',
        default_value='30.0',
        description='Camera frame rate'
    )
    
    pixel_format_arg = DeclareLaunchArgument(
        'pixel_format',
        default_value='YUYV',
        description='Camera pixel format'
    )
    
    publish_compressed_arg = DeclareLaunchArgument(
        'publish_compressed',
        default_value='true',
        description='Publish compressed image topic'
    )
    
    compressed_frame_interval_arg = DeclareLaunchArgument(
        'compressed_frame_interval',
        default_value='6',
        description='Publish compressed image every N frames'
    )
    
    use_sim_time_arg = DeclareLaunchArgument(
        'use_sim_time',
        default_value='false',
        description='Use simulation time'
    )
    
    # Arm camera driver node
    arm_camera_driver_node = Node(
        package='maurice_cam',
        executable='arm_camera_driver',
        name='arm_camera_driver',
        output='screen',
        parameters=[
            {
                'camera_symlink': LaunchConfiguration('camera_symlink'),
                'width': LaunchConfiguration('width'),
                'height': LaunchConfiguration('height'),
                'fps': LaunchConfiguration('fps'),
                'pixel_format': LaunchConfiguration('pixel_format'),
                'publish_compressed': LaunchConfiguration('publish_compressed'),
                'compressed_frame_interval': LaunchConfiguration('compressed_frame_interval'),
                'use_sim_time': LaunchConfiguration('use_sim_time')
            }
        ],
        remappings=[
            # You can add remappings here if needed
        ]
    )
    
    return LaunchDescription([
        camera_symlink_arg,
        width_arg,
        height_arg,
        fps_arg,
        pixel_format_arg,
        publish_compressed_arg,
        compressed_frame_interval_arg,
        use_sim_time_arg,
        arm_camera_driver_node
    ])
