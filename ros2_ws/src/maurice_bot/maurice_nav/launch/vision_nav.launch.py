#!/usr/bin/env python3
"""
Vision-Enhanced Navigation Launch File

Launches the full Nav2 navigation stack with vision-based obstacle detection.
This combines:
1. Vision Obstacle Detector - Detects objects from camera and publishes PointCloud2
2. Nav2 Stack - Planner, Controller, BT Navigator with vision-enhanced costmaps

Usage:
    ros2 launch maurice_nav vision_nav.launch.py
    ros2 launch maurice_nav vision_nav.launch.py model:=gemini-3-flash
    ros2 launch maurice_nav vision_nav.launch.py detection_rate:=5.0
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    package_name = 'maurice_nav'
    share_dir = get_package_share_directory(package_name)
    
    # Config files
    planner_params_file = os.path.join(share_dir, 'config', 'planner.yaml')
    controller_params_file = os.path.join(share_dir, 'config', 'controller.yaml')
    costmap_params_file = os.path.join(share_dir, 'config', 'costmap_with_vision.yaml')
    bt_navigator_params_file = os.path.join(share_dir, 'config', 'bt_navigator.yaml')
    behavior_params_file = os.path.join(share_dir, 'config', 'behavior.yaml')
    smoother_params_file = os.path.join(share_dir, 'config', 'velocity_smoother.yaml')
    
    # Launch arguments
    model_arg = DeclareLaunchArgument(
        'model',
        default_value='gemini-2.0-flash',
        description='Gemini model for object detection'
    )
    
    detection_rate_arg = DeclareLaunchArgument(
        'detection_rate',
        default_value='2.0',
        description='Object detection rate in Hz'
    )
    
    camera_topic_arg = DeclareLaunchArgument(
        'camera_topic',
        default_value='/mars/main_camera/image',
        description='Camera image topic'
    )
    
    # Static transform: map -> odom (identity for mapfree navigation)
    static_tf = Node(
        package='tf2_ros',
        executable='static_transform_publisher',
        name='static_map_to_odom',
        arguments=['0', '0', '0', '0', '0', '0', 'map', 'odom'],
        output='screen'
    )
    
    # Vision Obstacle Detector Node
    vision_detector_node = Node(
        package='maurice_nav',
        executable='vision_obstacle_detector',
        name='vision_obstacle_detector',
        output='screen',
        parameters=[{
            'model': LaunchConfiguration('model'),
            'detection_rate': LaunchConfiguration('detection_rate'),
            'camera_topic': LaunchConfiguration('camera_topic'),
            'output_topic': '/camera/obstacle_points',
            'camera_height': 0.18,
            'camera_pitch': -15.0,
            'camera_fov_h': 150.0,
            'camera_fov_v': 120.0,
            'max_detection_range': 3.0,
            'min_confidence': 0.5,
        }]
    )
    
    # Nav2 Planner Server
    planner_node = Node(
        package='nav2_planner',
        executable='planner_server',
        name='planner_server',
        output='screen',
        parameters=[planner_params_file, costmap_params_file]
    )
    
    # Nav2 Controller Server
    controller_node = Node(
        package='nav2_controller',
        executable='controller_server',
        name='controller_server',
        output='screen',
        parameters=[controller_params_file, costmap_params_file],
        remappings=[('cmd_vel', 'cmd_vel_raw')]
    )
    
    # Velocity Smoother
    velocity_smoother_node = Node(
        package='nav2_velocity_smoother',
        executable='velocity_smoother',
        name='velocity_smoother',
        output='screen',
        parameters=[smoother_params_file],
        remappings=[('cmd_vel', '/cmd_vel_raw'), ('cmd_vel_smoothed', '/cmd_vel')]
    )
    
    # BT Navigator
    nav_to_pose_bt_xml = os.path.join(share_dir, 'config', 'nav_to_pose.xml')
    nav_through_poses_bt_xml = os.path.join(share_dir, 'config', 'nav_through_poses.xml')
    
    bt_navigator_node = Node(
        package='nav2_bt_navigator',
        executable='bt_navigator',
        name='bt_navigator',
        output='screen',
        parameters=[
            bt_navigator_params_file,
            {'default_nav_to_pose_bt_xml': nav_to_pose_bt_xml},
            {'default_nav_through_poses_bt_xml': nav_through_poses_bt_xml}
        ]
    )
    
    # Behavior Server
    behavior_server_node = Node(
        package='nav2_behaviors',
        executable='behavior_server',
        name='behavior_server',
        output='screen',
        parameters=[behavior_params_file],
        remappings=[('cmd_vel', 'cmd_vel_raw')]
    )
    
    # Lifecycle Manager
    lifecycle_manager_node = Node(
        package='nav2_lifecycle_manager',
        executable='lifecycle_manager',
        name='lifecycle_manager',
        output='screen',
        parameters=[{
            'autostart': True,
            'node_names': [
                'planner_server',
                'controller_server',
                'bt_navigator',
                'behavior_server',
                'velocity_smoother'
            ]
        }]
    )
    
    return LaunchDescription([
        # Launch arguments
        model_arg,
        detection_rate_arg,
        camera_topic_arg,
        # Nodes
        static_tf,
        vision_detector_node,
        planner_node,
        controller_node,
        velocity_smoother_node,
        bt_navigator_node,
        behavior_server_node,
        lifecycle_manager_node,
    ])

