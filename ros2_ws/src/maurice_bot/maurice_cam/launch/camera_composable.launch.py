#!/usr/bin/env python3
"""
Composable node launch file for maurice_cam.

This launch file runs all camera nodes in a single process using ROS 2 composition.
With intra-process communication enabled, image data is passed via shared_ptr
instead of being serialized, eliminating copy overhead between nodes.

Usage:
    ros2 launch maurice_cam camera_composable.launch.py

    # With options:
    ros2 launch maurice_cam camera_composable.launch.py launch_main_camera:=true launch_arm_camera:=true launch_webrtc:=true
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition
from launch.substitutions import LaunchConfiguration, PythonExpression
from launch_ros.actions import ComposableNodeContainer
from launch_ros.descriptions import ComposableNode


def generate_launch_description():
    # Declare launch arguments
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
    
    launch_webrtc_arg = DeclareLaunchArgument(
        'launch_webrtc',
        default_value='true',
        description='Launch the WebRTC streamer'
    )
    
    use_sim_time_arg = DeclareLaunchArgument(
        'use_sim_time',
        default_value='false',
        description='Use simulation time'
    )

    # Main camera parameters
    main_camera_symlink_arg = DeclareLaunchArgument(
        'main_camera_symlink',
        default_value='3D',
        description='Main camera device symlink pattern (searches for symlinks containing this pattern)'
    )
    
    main_camera_width_arg = DeclareLaunchArgument(
        'main_camera_width',
        default_value='2560',
        description='Main camera capture width (full FOV)'
    )
    
    main_camera_height_arg = DeclareLaunchArgument(
        'main_camera_height',
        default_value='720',
        description='Main camera capture height (full FOV)'
    )
    
    main_camera_publish_left_width_arg = DeclareLaunchArgument(
        'main_camera_publish_left_width',
        default_value='640',
        description='Main camera publish left image width'
    )
    
    main_camera_publish_left_height_arg = DeclareLaunchArgument(
        'main_camera_publish_left_height',
        default_value='480',
        description='Main camera publish left image height'
    )
    
    main_camera_publish_stereo_width_arg = DeclareLaunchArgument(
        'main_camera_publish_stereo_width',
        default_value='1280',
        description='Main camera publish stereo image width'
    )
    
    main_camera_publish_stereo_height_arg = DeclareLaunchArgument(
        'main_camera_publish_stereo_height',
        default_value='480',
        description='Main camera publish stereo image height'
    )
    
    main_camera_fps_arg = DeclareLaunchArgument(
        'main_camera_fps',
        default_value='30.0',
        description='Main camera framerate'
    )
    
    main_camera_frame_id_arg = DeclareLaunchArgument(
        'main_camera_frame_id',
        default_value='camera_optical_frame',
        description='Main camera frame ID'
    )
    
    main_camera_jpeg_quality_arg = DeclareLaunchArgument(
        'main_camera_jpeg_quality',
        default_value='80',
        description='Main camera JPEG compression quality (1-100)'
    )
    
    main_camera_publish_compressed_arg = DeclareLaunchArgument(
        'main_camera_publish_compressed',
        default_value='true',
        description='Main camera publish compressed image topic'
    )
    
    main_camera_compressed_frame_interval_arg = DeclareLaunchArgument(
        'main_camera_compressed_frame_interval',
        default_value='6',
        description='Main camera publish compressed image every N frames'
    )
    
    main_camera_exposure_arg = DeclareLaunchArgument(
        'main_camera_exposure',
        default_value='-1',
        description='Main camera manual exposure time (-1 = use current value, 1-10000)'
    )
    
    main_camera_gain_arg = DeclareLaunchArgument(
        'main_camera_gain',
        default_value='-1',
        description='Main camera manual gain value (-1 = use current value, 0-255)'
    )
    
    main_camera_disable_auto_exposure_arg = DeclareLaunchArgument(
        'main_camera_disable_auto_exposure',
        default_value='false',
        description='Main camera disable automatic exposure (true/false)'
    )
    
    main_camera_default_gain_arg = DeclareLaunchArgument(
        'main_camera_default_gain',
        default_value='110',
        description='Main camera default gain value for auto-exposure mode (0-255)'
    )
    
    main_camera_enable_auto_exposure_arg = DeclareLaunchArgument(
        'main_camera_enable_auto_exposure',
        default_value='false',
        description='Main camera enable custom auto exposure algorithm (true/false)'
    )
    
    main_camera_target_brightness_arg = DeclareLaunchArgument(
        'main_camera_target_brightness',
        default_value='128.0',
        description='Main camera target brightness for auto exposure (0-255, 128 = 18% gray)'
    )
    
    main_camera_ae_kp_arg = DeclareLaunchArgument(
        'main_camera_ae_kp',
        default_value='0.8',
        description='Main camera auto exposure proportional gain (0.5-1.5, higher = faster response, 0.8 recommended for robots)'
    )
    
    main_camera_auto_exposure_update_interval_arg = DeclareLaunchArgument(
        'main_camera_auto_exposure_update_interval',
        default_value='3',
        description='Main camera auto exposure update interval (update every N frames, 3 = 10Hz for 30Hz camera)'
    )

    # Stereo depth estimator parameters
    launch_depth_estimator_arg = DeclareLaunchArgument(
        'launch_depth_estimator',
        default_value='false',  # Temporarily disabled
        description='Launch the stereo depth estimator'
    )
    
    depth_data_directory_arg = DeclareLaunchArgument(
        'depth_data_directory',
        default_value='/home/jetson1/innate-os/data',
        description='Data directory containing calibration config'
    )
    
    
    depth_output_topic_arg = DeclareLaunchArgument(
        'depth_output_topic',
        default_value='/mars/main_camera/depth',
        description='Output depth image topic'
    )
    
    depth_disparity_topic_arg = DeclareLaunchArgument(
        'depth_disparity_topic',
        default_value='/mars/main_camera/disparity',
        description='Output disparity image topic'
    )
    
    depth_max_disparity_arg = DeclareLaunchArgument(
        'depth_max_disparity',
        default_value='64',
        description='Maximum disparity for stereo matching'
    )
    
    depth_publish_disparity_arg = DeclareLaunchArgument(
        'depth_publish_disparity',
        default_value='false',
        description='Publish disparity visualization image'
    )
    
    depth_stereo_width_arg = DeclareLaunchArgument(
        'depth_stereo_width',
        default_value='1280',
        description='Expected stereo image width (full stereo, left+right)'
    )
    
    depth_stereo_height_arg = DeclareLaunchArgument(
        'depth_stereo_height',
        default_value='480',
        description='Expected stereo image height'
    )
    
    depth_stereo_topic_arg = DeclareLaunchArgument(
        'depth_stereo_topic',
        default_value='/mars/main_camera/stereo',
        description='Input stereo image topic for depth estimation'
    )

    # Arm camera parameters
    arm_camera_symlink_arg = DeclareLaunchArgument(
        'arm_camera_symlink',
        default_value='Arducam',
        description='Arm camera device symlink pattern (searches for symlinks containing this pattern)'
    )
    
    arm_camera_width_arg = DeclareLaunchArgument(
        'arm_camera_width',
        default_value='640',
        description='Arm camera capture width'
    )
    
    arm_camera_height_arg = DeclareLaunchArgument(
        'arm_camera_height',
        default_value='480',
        description='Arm camera capture height'
    )
    
    arm_camera_fps_arg = DeclareLaunchArgument(
        'arm_camera_fps',
        default_value='30.0',
        description='Arm camera framerate'
    )
    
    arm_camera_pixel_format_arg = DeclareLaunchArgument(
        'arm_camera_pixel_format',
        default_value='YUYV',
        description='Arm camera pixel format'
    )
    
    arm_camera_publish_compressed_arg = DeclareLaunchArgument(
        'arm_camera_publish_compressed',
        default_value='true',
        description='Arm camera publish compressed image topic'
    )
    
    arm_camera_compressed_frame_interval_arg = DeclareLaunchArgument(
        'arm_camera_compressed_frame_interval',
        default_value='6',
        description='Arm camera publish compressed image every N frames'
    )

    # Build list of composable nodes based on launch arguments
    composable_nodes = []
    
    # Main camera driver node
    main_camera_node = ComposableNode(
        package='maurice_cam',
        plugin='maurice_cam::MainCameraDriver',
        name='main_camera_driver',
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'camera_symlink': LaunchConfiguration('main_camera_symlink'),
            'width': LaunchConfiguration('main_camera_width'),
            'height': LaunchConfiguration('main_camera_height'),
            'publish_left_width': LaunchConfiguration('main_camera_publish_left_width'),
            'publish_left_height': LaunchConfiguration('main_camera_publish_left_height'),
            'publish_stereo_width': LaunchConfiguration('main_camera_publish_stereo_width'),
            'publish_stereo_height': LaunchConfiguration('main_camera_publish_stereo_height'),
            'fps': LaunchConfiguration('main_camera_fps'),
            'frame_id': LaunchConfiguration('main_camera_frame_id'),
            'jpeg_quality': LaunchConfiguration('main_camera_jpeg_quality'),
            'publish_compressed': LaunchConfiguration('main_camera_publish_compressed'),
            'compressed_frame_interval': LaunchConfiguration('main_camera_compressed_frame_interval'),
            'exposure': LaunchConfiguration('main_camera_exposure'),
            'gain': LaunchConfiguration('main_camera_gain'),
            'disable_auto_exposure': LaunchConfiguration('main_camera_disable_auto_exposure'),
            'default_gain': LaunchConfiguration('main_camera_default_gain'),
            'enable_auto_exposure': LaunchConfiguration('main_camera_enable_auto_exposure'),
            'target_brightness': LaunchConfiguration('main_camera_target_brightness'),
            'ae_kp': LaunchConfiguration('main_camera_ae_kp'),
            'auto_exposure_update_interval': LaunchConfiguration('main_camera_auto_exposure_update_interval'),
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )
    
    # Arm camera driver node
    arm_camera_node = ComposableNode(
        package='maurice_cam',
        plugin='maurice_cam::ArmCameraDriver',
        name='arm_camera_driver',
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'camera_symlink': LaunchConfiguration('arm_camera_symlink'),
            'width': LaunchConfiguration('arm_camera_width'),
            'height': LaunchConfiguration('arm_camera_height'),
            'fps': LaunchConfiguration('arm_camera_fps'),
            'pixel_format': LaunchConfiguration('arm_camera_pixel_format'),
            'publish_compressed': LaunchConfiguration('arm_camera_publish_compressed'),
            'compressed_frame_interval': LaunchConfiguration('arm_camera_compressed_frame_interval'),
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )
    
    # WebRTC streamer node
    webrtc_node = ComposableNode(
        package='maurice_cam',
        plugin='maurice_cam::WebRTCStreamer',
        name='webrtc_streamer',
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'live_main_camera_topic': '/mars/main_camera/image',
            'live_arm_camera_topic': '/mars/arm/image_raw',
            'replay_main_camera_topic': '/brain/recorder/replay/main_camera/image',
            'replay_arm_camera_topic': '/brain/recorder/replay/arm_camera/image_raw',
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )
    
    # Stereo depth estimator node
    depth_estimator_node = ComposableNode(
        package='maurice_cam',
        plugin='maurice_cam::StereoDepthEstimator',
        name='stereo_depth_estimator',
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'data_directory': LaunchConfiguration('depth_data_directory'),
            'stereo_topic': LaunchConfiguration('depth_stereo_topic'),
            'depth_topic': LaunchConfiguration('depth_output_topic'),
            'disparity_topic': LaunchConfiguration('depth_disparity_topic'),
            'frame_id': LaunchConfiguration('main_camera_frame_id'),
            'max_disparity': LaunchConfiguration('depth_max_disparity'),
            'publish_disparity': LaunchConfiguration('depth_publish_disparity'),
            'stereo_width': LaunchConfiguration('depth_stereo_width'),
            'stereo_height': LaunchConfiguration('depth_stereo_height'),
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

    # Create the composable node container
    # All nodes run in the same process, enabling zero-copy intra-process communication
    container = ComposableNodeContainer(
        name='camera_container',
        namespace='',
        package='rclcpp_components',
        executable='component_container_mt',  # Multi-threaded container for parallel callbacks
        composable_node_descriptions=[
            main_camera_node,
            arm_camera_node,
            webrtc_node,
            # depth_estimator_node,  # Temporarily disabled for calibration
        ],
        output='screen',
        emulate_tty=True,
    )

    return LaunchDescription([
        # Launch arguments
        launch_main_camera_arg,
        launch_arm_camera_arg,
        launch_webrtc_arg,
        use_sim_time_arg,
        # Main camera arguments
        main_camera_symlink_arg,
        main_camera_width_arg,
        main_camera_height_arg,
        main_camera_publish_left_width_arg,
        main_camera_publish_left_height_arg,
        main_camera_publish_stereo_width_arg,
        main_camera_publish_stereo_height_arg,
        main_camera_fps_arg,
        main_camera_frame_id_arg,
        main_camera_jpeg_quality_arg,
        main_camera_publish_compressed_arg,
        main_camera_compressed_frame_interval_arg,
        main_camera_exposure_arg,
        main_camera_gain_arg,
        main_camera_disable_auto_exposure_arg,
        main_camera_default_gain_arg,
        main_camera_enable_auto_exposure_arg,
        main_camera_target_brightness_arg,
        main_camera_ae_kp_arg,
        main_camera_auto_exposure_update_interval_arg,
        # Stereo depth estimator arguments
        launch_depth_estimator_arg,
        depth_data_directory_arg,
        depth_stereo_topic_arg,
        depth_output_topic_arg,
        depth_disparity_topic_arg,
        depth_max_disparity_arg,
        depth_publish_disparity_arg,
        depth_stereo_width_arg,
        depth_stereo_height_arg,
        # Arm camera arguments
        arm_camera_symlink_arg,
        arm_camera_width_arg,
        arm_camera_height_arg,
        arm_camera_fps_arg,
        arm_camera_pixel_format_arg,
        arm_camera_publish_compressed_arg,
        arm_camera_compressed_frame_interval_arg,
        # Container with all nodes
        container,
    ])
