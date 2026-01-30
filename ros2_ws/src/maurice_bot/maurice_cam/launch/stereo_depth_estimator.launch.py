#!/usr/bin/env python3
"""
Launch file for the stereo depth estimator node.

This launch file runs the stereo depth estimator as a standalone node or as a
composable node with intra-process communication enabled.

Uses VPI Block Matching algorithm with:
- Downscaling to calibration resolution for processing
- OpenCV rectification (accurate)
- VPI Block Matching (fast, good quality)
- No post-filtering (cleanest results)
- Upscaling depth back to input resolution

Usage:
    # Standalone node (subscribes to existing stereo topic)
    ros2 launch maurice_cam stereo_depth_estimator.launch.py

    # With composable mode for zero-copy IPC (when main camera is in same container)
    ros2 launch maurice_cam stereo_depth_estimator.launch.py use_composable:=true
    
    # With custom config
    ros2 launch maurice_cam stereo_depth_estimator.launch.py config:=/path/to/config.yaml
"""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.conditions import IfCondition, UnlessCondition
from launch.substitutions import LaunchConfiguration, PathJoinSubstitution
from launch_ros.actions import Node, ComposableNodeContainer
from launch_ros.descriptions import ComposableNode
from launch_ros.substitutions import FindPackageShare
from ament_index_python.packages import get_package_share_directory
import os


def generate_launch_description():
    # Default config file path
    default_config = PathJoinSubstitution([
        FindPackageShare('maurice_cam'),
        'config',
        'stereo_depth_estimator.yaml'
    ])
    
    # Declare launch arguments
    use_composable_arg = DeclareLaunchArgument(
        'use_composable',
        default_value='false',
        description='Run as composable node in a container (enables zero-copy IPC)'
    )
    
    use_sim_time_arg = DeclareLaunchArgument(
        'use_sim_time',
        default_value='false',
        description='Use simulation time'
    )
    
    config_arg = DeclareLaunchArgument(
        'config',
        default_value=default_config,
        description='Path to config YAML file'
    )

    # Standalone node (default)
    standalone_node = Node(
        package='maurice_cam',
        executable='stereo_depth_estimator',
        name='stereo_depth_estimator',
        parameters=[
            LaunchConfiguration('config'),
            {'use_sim_time': LaunchConfiguration('use_sim_time')}
        ],
        output='screen',
        emulate_tty=True,
        condition=UnlessCondition(LaunchConfiguration('use_composable')),
    )

    # Composable node in container (for zero-copy IPC)
    composable_container = ComposableNodeContainer(
        name='depth_estimator_container',
        namespace='',
        package='rclcpp_components',
        executable='component_container_mt',
        composable_node_descriptions=[
            ComposableNode(
                package='maurice_cam',
                plugin='maurice_cam::StereoDepthEstimator',
                name='stereo_depth_estimator',
                parameters=[
                    LaunchConfiguration('config'),
                    {'use_sim_time': LaunchConfiguration('use_sim_time')}
                ],
                extra_arguments=[{'use_intra_process_comms': True}],
            ),
        ],
        output='screen',
        emulate_tty=True,
        condition=IfCondition(LaunchConfiguration('use_composable')),
    )

    return LaunchDescription([
        # Launch arguments
        use_composable_arg,
        use_sim_time_arg,
        config_arg,
        # Nodes (one will be active based on use_composable)
        standalone_node,
        composable_container,
    ])
