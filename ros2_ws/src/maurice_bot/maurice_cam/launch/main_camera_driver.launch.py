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
        default_value='3D',
        description='Camera symlink pattern (searches for symlinks containing this pattern in /dev/v4l/by-id/)'
    )
    
    width_arg = DeclareLaunchArgument(
        'width',
        default_value='1280',
        description='Camera capture width (640x480 per side)'
    )
    
    height_arg = DeclareLaunchArgument(
        'height',
        default_value='480',
        description='Camera capture height (640x480 per side)'
    )
    
    publish_left_width_arg = DeclareLaunchArgument(
        'publish_left_width',
        default_value='640',
        description='Publish left image width'
    )
    
    publish_left_height_arg = DeclareLaunchArgument(
        'publish_left_height',
        default_value='480',
        description='Publish left image height'
    )
    
    publish_stereo_width_arg = DeclareLaunchArgument(
        'publish_stereo_width',
        default_value='1280',
        description='Publish stereo image width'
    )
    
    publish_stereo_height_arg = DeclareLaunchArgument(
        'publish_stereo_height',
        default_value='480',
        description='Publish stereo image height'
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
    
    default_gain_arg = DeclareLaunchArgument(
        'default_gain',
        default_value='110',
        description='Default gain value for auto-exposure mode (0-255)'
    )
    
    # Auto exposure parameters
    auto_exposure_mode_arg = DeclareLaunchArgument(
        'auto_exposure_mode',
        default_value='0',
        description='Auto exposure mode: 0=hardware AE, 1=custom PID AE, 2=manual'
    )
    
    target_brightness_arg = DeclareLaunchArgument(
        'target_brightness',
        default_value='128.0',
        description='Target brightness for auto exposure (0-255, 128 = 18% gray)'
    )
    
    ae_kp_arg = DeclareLaunchArgument(
        'ae_kp',
        default_value='0.8',
        description='Auto exposure proportional gain (0.5-1.5, higher = faster response, 0.8 recommended for robots)'
    )
    
    auto_exposure_update_interval_arg = DeclareLaunchArgument(
        'auto_exposure_update_interval',
        default_value='3',
        description='Auto exposure update interval (update every N frames, 3 = 10Hz for 30Hz camera)'
    )
    
    use_sim_time_arg = DeclareLaunchArgument(
        'use_sim_time',
        default_value='false',
        description='Use simulation time'
    )
    
    # Main camera driver node
    main_camera_driver_node = Node(
        package='maurice_cam',
        executable='main_camera_driver',
        name='main_camera_driver',
        output='screen',
        parameters=[
            {
                'camera_symlink': LaunchConfiguration('camera_symlink'),
                'width': LaunchConfiguration('width'),
                'height': LaunchConfiguration('height'),
                'publish_left_width': LaunchConfiguration('publish_left_width'),
                'publish_left_height': LaunchConfiguration('publish_left_height'),
                'publish_stereo_width': LaunchConfiguration('publish_stereo_width'),
                'publish_stereo_height': LaunchConfiguration('publish_stereo_height'),
                'fps': LaunchConfiguration('fps'),
                'frame_id': LaunchConfiguration('frame_id'),
                'jpeg_quality': LaunchConfiguration('jpeg_quality'),
                'publish_compressed': LaunchConfiguration('publish_compressed'),
                'compressed_frame_interval': LaunchConfiguration('compressed_frame_interval'),
                'exposure': LaunchConfiguration('exposure'),
                'gain': LaunchConfiguration('gain'),
                'default_gain': LaunchConfiguration('default_gain'),
                'auto_exposure_mode': LaunchConfiguration('auto_exposure_mode'),
                'target_brightness': LaunchConfiguration('target_brightness'),
                'ae_kp': LaunchConfiguration('ae_kp'),
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
        publish_left_width_arg,
        publish_left_height_arg,
        publish_stereo_width_arg,
        publish_stereo_height_arg,
        fps_arg,
        frame_id_arg,
        jpeg_quality_arg,
        publish_compressed_arg,
        compressed_frame_interval_arg,
        exposure_arg,
        gain_arg,
        default_gain_arg,
        auto_exposure_mode_arg,
        target_brightness_arg,
        ae_kp_arg,
        auto_exposure_update_interval_arg,
        use_sim_time_arg,
        main_camera_driver_node
    ])

