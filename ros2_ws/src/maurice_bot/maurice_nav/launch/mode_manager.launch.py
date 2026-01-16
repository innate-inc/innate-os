#!/usr/bin/env python3

import os

from ament_index_python.packages import get_package_share_directory

from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node


def expand_action_remappings(action_remappings):
    """
    Expand action remappings into their underlying topic/service remappings.
    
    Each ROS2 action consists of 5 topics/services:
    - _action/feedback (topic)
    - _action/status (topic)
    - _action/cancel_goal (service)
    - _action/get_result (service)
    - _action/send_goal (service)
    
    Args:
        action_remappings: List of tuples [(from_action, to_action), ...]
    
    Returns:
        List of expanded topic/service remappings
    """
    expanded = []
    action_suffixes = [
        '_action/feedback',
        '_action/status',
        '_action/cancel_goal',
        '_action/get_result',
        '_action/send_goal',
    ]
    
    for from_action, to_action in action_remappings:
        for suffix in action_suffixes:
            expanded.append((f'{from_action}/{suffix}', f'{to_action}/{suffix}'))
    
    return expanded


def generate_launch_description():
    package_name = 'maurice_nav'
    share_dir = get_package_share_directory(package_name)
    maurice_nav_launch_dir = os.path.join(share_dir, 'launch')
    
    # Shared configuration files
    bt_navigator_params_file = os.path.join(share_dir, 'config', 'bt_navigator.yaml')
    behavior_params_file = os.path.join(share_dir, 'config', 'behavior.yaml')
    smoother_params_file = os.path.join(share_dir, 'config', 'velocity_smoother.yaml')
    planner_params_file = os.path.join(share_dir, 'config', 'planner.yaml')
    costmap_params_file = os.path.join(share_dir, 'config', 'costmap.yaml')
    
    # BT XML paths
    nav_to_pose_bt_xml = os.path.join(share_dir, 'config', 'nav_to_pose.xml')
    nav_through_poses_bt_xml = os.path.join(share_dir, 'config', 'nav_through_poses.xml')
    nav_to_pose_mapfree_bt_xml = os.path.join(share_dir, 'config', 'nav_to_pose_mapfree.xml')
    
    navigation_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource([maurice_nav_launch_dir, '/navigation.launch.py'])
    )
    
    # mapfree_launch = IncludeLaunchDescription(
    #     PythonLaunchDescriptionSource([maurice_nav_launch_dir, '/mapfree_local_nav.launch.py'])
    # )
    
    mapping_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource([maurice_nav_launch_dir, '/mapping.launch.py'])
    )
    
    # Shared velocity smoother node
    velocity_smoother_node = Node(
        package='nav2_velocity_smoother',
        executable='velocity_smoother',
        name='velocity_smoother',
        output='screen',
        parameters=[smoother_params_file],
        remappings=[('cmd_vel', '/cmd_vel_raw'), ('cmd_vel_smoothed', '/cmd_vel')]
    )
    
    # Shared BT navigator node
    bt_navigator_node = Node(
        package='nav2_bt_navigator',
        executable='bt_navigator',
        name='bt_navigator',
        output='screen',
        parameters=[
            bt_navigator_params_file,
            {'default_nav_to_pose_bt_xml': nav_to_pose_bt_xml},
            {'default_nav_through_poses_bt_xml': nav_through_poses_bt_xml}
        ],
        remappings=expand_action_remappings([
            ('navigate_to_pose', 'internal_navigate_to_pose'),
        ])
    )
    
    # Shared behavior server node
    behavior_server_node = Node(
        package='nav2_behaviors',
        executable='behavior_server',
        name='behavior_server',
        output='screen',
        parameters=[behavior_params_file],
        arguments=['--ros-args', '--log-level', 'warn'],
        remappings=[('cmd_vel', 'cmd_vel_raw')]
    )
    
    # Mapfree planner node (runs in mapfree namespace)
    mapfree_planner_node = Node(
        package='nav2_planner',
        executable='planner_server',
        name='planner_server',
        namespace='mapfree',
        output='screen',
        parameters=[planner_params_file, costmap_params_file],
        remappings=[
        # TF remappings - critical for namespaced nodes
        ('tf', '/tf'),
        ('tf_static', '/tf_static'),
    ]
    )
    
    # Null map node for identity map->odom transform
    null_map_node = Node(
        package='maurice_nav',
        executable='null_map_node',
        name='null_map_node',
        output='screen'
    )
    
    return LaunchDescription([
        null_map_node,
        navigation_launch,
        # mapfree_launch,
        mapping_launch,
        velocity_smoother_node,
        bt_navigator_node,
        mapfree_planner_node,
        behavior_server_node,
        Node(
            package='maurice_nav',
            executable='mode_manager.py',
            name='mode_manager',
            output='screen',
            parameters=[]
        )
    ]) 