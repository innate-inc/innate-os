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
        default_value='usb-3D_USB_Camera_3D_USB_Camera_01.00.00-video-index0',
        description='Camera symlink name in /dev/v4l/by-id/'
    )
    
    width_arg = DeclareLaunchArgument(
        'width',
        default_value='1280',
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
    
    frame_id_arg = DeclareLaunchArgument(
        'frame_id',
        default_value='camera_optical_frame',
        description='Camera frame ID'
    )
    
    jpeg_quality_arg = DeclareLaunchArgument(
        'jpeg_quality',
        default_value='80',
        description='JPEG compression quality (1-100)'
    )
    
    # V4L2 control parameters
    exposure_arg = DeclareLaunchArgument(
        'exposure',
        default_value='-1',
        description='Manual exposure time (-1 = use current value, 1-10000)'
    )
    
    gain_arg = DeclareLaunchArgument(
        'gain',
        default_value='-1',
        description='Manual gain value (-1 = use current value, 0-255)'
    )
    
    disable_auto_exposure_arg = DeclareLaunchArgument(
        'disable_auto_exposure',
        default_value='false',
        description='Disable automatic exposure (true/false)'
    )
    
    default_gain_arg = DeclareLaunchArgument(
        'default_gain',
        default_value='110',
        description='Default gain value for auto-exposure mode (0-255)'
    )
    
    use_sim_time_arg = DeclareLaunchArgument(
        'use_sim_time',
        default_value='false',
        description='Use simulation time'
    )
    
    # Camera driver node
    camera_driver_node = Node(
        package='maurice_cam',
        executable='camera_driver',
        name='camera_driver',
        output='screen',
        parameters=[
            {
                'camera_symlink': LaunchConfiguration('camera_symlink'),
                'width': LaunchConfiguration('width'),
                'height': LaunchConfiguration('height'),
                'fps': LaunchConfiguration('fps'),
                'frame_id': LaunchConfiguration('frame_id'),
                'jpeg_quality': LaunchConfiguration('jpeg_quality'),
                'exposure': LaunchConfiguration('exposure'),
                'gain': LaunchConfiguration('gain'),
                'disable_auto_exposure': LaunchConfiguration('disable_auto_exposure'),
                'default_gain': LaunchConfiguration('default_gain'),
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
        frame_id_arg,
        jpeg_quality_arg,
        exposure_arg,
        gain_arg,
        disable_auto_exposure_arg,
        default_gain_arg,
        use_sim_time_arg,
        camera_driver_node
    ])
