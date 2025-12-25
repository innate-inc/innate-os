#!/usr/bin/env python3

import os
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    # Get package share directory
    pkg_share = get_package_share_directory('maurice_cam')
    
    # Declare launch arguments for enabling/disabling cameras
    launch_main_camera_arg = DeclareLaunchArgument(
        'launch_main_camera',
        default_value='true',
        description='Launch the main camera driver'
    )
    
    launch_arm_camera_arg = DeclareLaunchArgument(
        'launch_arm_camera',
        default_value='true',
        description='Launch the arm camera driver'
    )
    
    use_sim_time_arg = DeclareLaunchArgument(
        'use_sim_time',
        default_value='false',
        description='Use simulation time'
    )
    
    # Include main camera driver launch file
    main_camera_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_share, 'launch', 'main_camera_driver.launch.py')
        ),
        launch_arguments={
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'camera_symlink': 'usb-3D_USB_Camera_3D_USB_Camera_01.00.00-video-index0',
            'width': '1280',
            'height': '480',
            'fps': '30.0',
        }.items()
    )
    
    # Include arm camera driver launch file
    arm_camera_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(
            os.path.join(pkg_share, 'launch', 'arm_camera_driver.launch.py')
        ),
        launch_arguments={
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'camera_symlink': 'usb-Arducam_Technology_Co.__Ltd._Arducam_USB_Camera_UC684-video-index0',
            'width': '640',
            'height': '480',
            'fps': '30.0',
            'pixel_format': 'YUYV',
        }.items()
    )
    
    return LaunchDescription([
        launch_main_camera_arg,
        launch_arm_camera_arg,
        use_sim_time_arg,
        main_camera_launch,
        arm_camera_launch
    ])

