#!/usr/bin/env python3

import os

from ament_index_python.packages import get_package_share_directory

from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node

def generate_launch_description():
    package_name = 'maurice_nav'
    share_dir = get_package_share_directory(package_name)
    maurice_nav_launch_dir = os.path.join(share_dir, 'launch')
    
    # Shared configuration files
    bt_navigator_params_file = os.path.join(share_dir, 'config', 'bt_navigator.yaml')
    behavior_params_file = os.path.join(share_dir, 'config', 'behavior.yaml')
    smoother_params_file = os.path.join(share_dir, 'config', 'velocity_smoother.yaml')
    
    # BT XML paths
    nav_to_pose_bt_xml = os.path.join(share_dir, 'config', 'nav_to_pose.xml')
    nav_through_poses_bt_xml = os.path.join(share_dir, 'config', 'nav_through_poses.xml')
    
    navigation_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource([maurice_nav_launch_dir, '/navigation.launch.py'])
    )
    
    mapfree_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource([maurice_nav_launch_dir, '/mapfree_local_nav.launch.py'])
    )
    
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
        ]
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
    
    return LaunchDescription([
        navigation_launch,
        mapfree_launch,
        mapping_launch,
        velocity_smoother_node,
        bt_navigator_node,
        behavior_server_node,
        Node(
            package='maurice_nav',
            executable='mode_manager.py',
            name='mode_manager',
            output='screen',
            parameters=[]
        )
    ]) 