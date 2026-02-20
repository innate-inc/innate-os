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
    
    capture_width_arg = DeclareLaunchArgument(
        'capture_width',
        default_value='1920',
        description='Camera capture width (native sensor resolution)'
    )
    
    capture_height_arg = DeclareLaunchArgument(
        'capture_height',
        default_value='1080',
        description='Camera capture height (native sensor resolution)'
    )
    
    publish_width_arg = DeclareLaunchArgument(
        'publish_width',
        default_value='640',
        description='Standard topic publish width (downscaled)'
    )
    
    publish_height_arg = DeclareLaunchArgument(
        'publish_height',
        default_value='480',
        description='Standard topic publish height (downscaled)'
    )
    
    fps_arg = DeclareLaunchArgument(
        'fps',
        default_value='30.0',
        description='Camera frame rate'
    )
    
    pixel_format_arg = DeclareLaunchArgument(
        'pixel_format',
        default_value='MJPG',
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
        description='Publish standard compressed image every N frames'
    )
    
    hires_compressed_frame_interval_arg = DeclareLaunchArgument(
        'hires_compressed_frame_interval',
        default_value='1',
        description='Publish high-res compressed image every N frames'
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
                'capture_width': LaunchConfiguration('capture_width'),
                'capture_height': LaunchConfiguration('capture_height'),
                'publish_width': LaunchConfiguration('publish_width'),
                'publish_height': LaunchConfiguration('publish_height'),
                'fps': LaunchConfiguration('fps'),
                'pixel_format': LaunchConfiguration('pixel_format'),
                'publish_compressed': LaunchConfiguration('publish_compressed'),
                'compressed_frame_interval': LaunchConfiguration('compressed_frame_interval'),
                'hires_compressed_frame_interval': LaunchConfiguration('hires_compressed_frame_interval'),
                'use_sim_time': LaunchConfiguration('use_sim_time')
            }
        ],
        remappings=[
            # You can add remappings here if needed
        ]
    )
    
    return LaunchDescription([
        camera_symlink_arg,
        capture_width_arg,
        capture_height_arg,
        publish_width_arg,
        publish_height_arg,
        fps_arg,
        pixel_format_arg,
        publish_compressed_arg,
        compressed_frame_interval_arg,
        hires_compressed_frame_interval_arg,
        use_sim_time_arg,
        arm_camera_driver_node
    ])
