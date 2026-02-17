#!/usr/bin/env python3
"""
Composable node launch file for maurice_cam.

All camera nodes run in a single process using ROS 2 composition.
Intra-process communication passes image data via shared_ptr (zero-copy).

Pipeline:
  MainCameraDriver → StereoDepthEstimator (VPI SGM + filters + depth + points)
                   → CameraInfo (published directly by MainCameraDriver)
  ArmCameraDriver
  WebRTCStreamer
  Remote throttle relays (lazy, 2 Hz) for RViz on a different machine

Usage:
    ros2 launch maurice_cam camera_composable.launch.py
"""

from launch import LaunchDescription
from launch.conditions import IfCondition
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import ComposableNodeContainer, Node
from launch_ros.descriptions import ComposableNode
from launch_ros.substitutions import FindPackageShare

from maurice_cam.remote_throttle_nodes import make_remote_throttle_nodes


def generate_launch_description():
    use_sim_time_arg = DeclareLaunchArgument(
        'use_sim_time',
        default_value='false',
        description='Use simulation time'
    )

    camera_config_arg = DeclareLaunchArgument(
        'camera_config',
        default_value=PathJoinSubstitution([
            FindPackageShare('maurice_cam'),
            'config',
            'stereo_depth_estimator.yaml'
        ]),
        description='Path to camera pipeline config file'
    )

    start_calibration_manager_arg = DeclareLaunchArgument(
        'start_calibration_manager',
        default_value='true',
        description='Start managed stereo calibration action server node'
    )

    # ── Nodes ─────────────────────────────────────────────────────────────────

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

    webrtc_node = ComposableNode(
        package='maurice_cam',
        plugin='maurice_cam::WebRTCStreamer',
        name='webrtc_streamer',
        parameters=[{
            'use_sim_time': LaunchConfiguration('use_sim_time'),
            'live_main_camera_topic': '/mars/main_camera/left/image_raw',
            'live_arm_camera_topic': '/mars/arm/image_raw',
            'replay_main_camera_topic': '/brain/recorder/replay/main_camera/left/image_raw',
            'replay_arm_camera_topic': '/brain/recorder/replay/arm_camera/image_raw',
        }],
        extra_arguments=[{'use_intra_process_comms': True}],
    )

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

    # ── Container ─────────────────────────────────────────────────────────────

    # ── Remote throttle relays (lazy, intra-process zero-copy input) ────────
    throttle_nodes = make_remote_throttle_nodes()

    # ── Container ─────────────────────────────────────────────────────────────

    camera_container = ComposableNodeContainer(
        name='camera_container',
        namespace='',
        package='rclcpp_components',
        executable='component_container_mt',
        composable_node_descriptions=[
            main_camera_node,
            arm_camera_node,
            webrtc_node,
            depth_estimator_node,
        ] + throttle_nodes,
        output='screen',
        emulate_tty=True,
    )

    stereo_calibration_manager = Node(
        package='maurice_cam',
        executable='stereo_calibrator',
        name='stereo_calibration_manager',
        output='screen',
        parameters=[
            {
                'use_sim_time': LaunchConfiguration('use_sim_time'),
                'interactive': False,
                'auto_start': False,
                'playback': False,
            }
        ],
        condition=IfCondition(LaunchConfiguration('start_calibration_manager')),
    )

    return LaunchDescription([
        use_sim_time_arg,
        camera_config_arg,
        start_calibration_manager_arg,
        camera_container,
        stereo_calibration_manager,
    ])
