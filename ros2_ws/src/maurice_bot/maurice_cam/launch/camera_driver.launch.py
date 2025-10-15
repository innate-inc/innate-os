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
    
    # Auto exposure parameters
    enable_auto_exposure_arg = DeclareLaunchArgument(
        'enable_auto_exposure',
        default_value='true',
        description='Enable custom auto exposure algorithm (true/false)'
    )
    
    target_brightness_arg = DeclareLaunchArgument(
        'target_brightness',
        default_value='128.0',
        description='Target brightness for auto exposure (0-255, 128 = 18% gray)'
    )
    
    ae_kp_arg = DeclareLaunchArgument(
        'ae_kp',
        default_value='0.8',
        description='Auto exposure PID proportional gain (higher = faster response, typical robot range: 0.6-1.2)'
    )
    
    ae_ki_arg = DeclareLaunchArgument(
        'ae_ki',
        default_value='0.05',
        description='Auto exposure PID integral gain (lower = more stable, typical robot range: 0.02-0.1)'
    )
    
    ae_kd_arg = DeclareLaunchArgument(
        'ae_kd',
        default_value='0.02',
        description='Auto exposure PID derivative gain (very low = smooth, typical robot range: 0.01-0.05)'
    )
    
    auto_exposure_update_interval_arg = DeclareLaunchArgument(
        'auto_exposure_update_interval',
        default_value='5',
        description='Auto exposure update interval (update every N frames)'
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
                'enable_auto_exposure': LaunchConfiguration('enable_auto_exposure'),
                'target_brightness': LaunchConfiguration('target_brightness'),
                'ae_kp': LaunchConfiguration('ae_kp'),
                'ae_ki': LaunchConfiguration('ae_ki'),
                'ae_kd': LaunchConfiguration('ae_kd'),
                'auto_exposure_update_interval': LaunchConfiguration('auto_exposure_update_interval'),
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
        enable_auto_exposure_arg,
        target_brightness_arg,
        ae_kp_arg,
        ae_ki_arg,
        ae_kd_arg,
        auto_exposure_update_interval_arg,
        use_sim_time_arg,
        camera_driver_node
    ])
