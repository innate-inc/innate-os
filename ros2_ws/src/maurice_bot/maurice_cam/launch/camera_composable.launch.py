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
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import ComposableNodeContainer
from launch_ros.descriptions import ComposableNode
from launch_ros.substitutions import FindPackageShare


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

    # Camera pipeline config file
    camera_config_arg = DeclareLaunchArgument(
        'camera_config',
        default_value=PathJoinSubstitution([
            FindPackageShare('maurice_cam'),
            'config',
            'stereo_depth_estimator.yaml'
        ]),
        description='Path to camera pipeline config file'
    )

    # Main camera driver node
    main_camera_node = ComposableNode(
        package='maurice_cam',
        plugin='maurice_cam::MainCameraDriver',
        name='main_camera_driver',
        parameters=[
            LaunchConfiguration('camera_config'),
            {'use_sim_time': LaunchConfiguration('use_sim_time')}
        ],
        extra_arguments=[{'use_intra_process_comms': True}],
    )
    
    # Arm camera driver node
    arm_camera_node = ComposableNode(
        package='maurice_cam',
        plugin='maurice_cam::ArmCameraDriver',
        name='arm_camera_driver',
        parameters=[
            LaunchConfiguration('camera_config'),
            {'use_sim_time': LaunchConfiguration('use_sim_time')}
        ],
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
        parameters=[
            LaunchConfiguration('camera_config'),
            {'use_sim_time': LaunchConfiguration('use_sim_time')}
        ],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

    # Point cloud generator node (using depth_image_proc for XYZRGB point cloud)
    pointcloud_generator_node = ComposableNode(
        package='depth_image_proc',
        plugin='depth_image_proc::PointCloudXyzrgbNode',
        name='pointcloud_generator',
        remappings=[
            ('depth_registered/image_rect', '/mars/main_camera/depth/image_rect_raw'),
            ('rgb/image_rect_color', '/mars/main_camera/image'),
            ('rgb/camera_info', '/mars/main_camera/depth/camera_info'),
            ('points', '/mars/main_camera/depth/points'),
        ],
        parameters=[
            LaunchConfiguration('camera_config'),
            {'use_sim_time': LaunchConfiguration('use_sim_time')}
        ],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

    # Point cloud CropBox filter node (using pcl_ros::CropBox)
    pointcloud_filter_node = ComposableNode(
        package='pcl_ros',
        plugin='pcl_ros::CropBox',
        name='pointcloud_cropbox_filter',
        remappings=[
            ('input', '/mars/main_camera/depth/points'),
            ('output', '/mars/main_camera/depth/points_filtered'),
        ],
        parameters=[
            LaunchConfiguration('camera_config'),
            {'use_sim_time': LaunchConfiguration('use_sim_time')}
        ],
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
            depth_estimator_node,
            pointcloud_generator_node,
            pointcloud_filter_node,
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
        # Camera pipeline config
        camera_config_arg,
        # Container with all nodes
        container,
    ])
