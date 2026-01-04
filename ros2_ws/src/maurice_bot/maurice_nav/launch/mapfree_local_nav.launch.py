#!/usr/bin/env python3

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    package_name = 'maurice_nav'
    share_dir = get_package_share_directory(package_name)

    planner_params_file = os.path.join(share_dir, 'config', 'planner.yaml')
    controller_params_file = os.path.join(share_dir, 'config', 'controller.yaml')
    costmap_params_file = os.path.join(share_dir, 'config', 'costmap_mapfree.yaml')
    bt_navigator_params_file = os.path.join(share_dir, 'config', 'bt_navigator.yaml')
    behavior_params_file = os.path.join(share_dir, 'config', 'behavior.yaml')
    smoother_params_file = os.path.join(share_dir, 'config', 'velocity_smoother.yaml')

    # Static transform publisher to provide an identity map->odom transform
    static_tf = Node(
        package='tf2_ros',
        executable='static_transform_publisher',
        name='static_map_to_odom',
        arguments=['0', '0', '0', '0', '0', '0', 'map', 'odom'],
        output='screen'
    )

    # Planner, Controller, Costmaps
    planner_node = Node(
        package='nav2_planner',
        executable='planner_server',
        name='mapfree_planner_server',
        output='screen',
        parameters=[planner_params_file, costmap_params_file]
    )

    controller_node = Node(
        package='nav2_controller',
        executable='controller_server',
        name='mapfree_controller_server',
        output='screen',
        parameters=[controller_params_file, costmap_params_file],
        remappings=[('cmd_vel', 'cmd_vel_raw')]
    )

    velocity_smoother_node = Node(
        package='nav2_velocity_smoother',
        executable='velocity_smoother',
        name='mapfree_velocity_smoother',
        output='screen',
        parameters=[smoother_params_file],
        remappings=[('cmd_vel', '/cmd_vel_raw'), ('cmd_vel_smoothed', '/cmd_vel')]
    )

    # Override BT XML paths with package-relative paths
    nav_to_pose_bt_xml = os.path.join(share_dir, 'config', 'nav_to_pose.xml')
    nav_through_poses_bt_xml = os.path.join(share_dir, 'config', 'nav_through_poses.xml')
    
    bt_navigator_node = Node(
        package='nav2_bt_navigator',
        executable='bt_navigator',
        name='mapfree_bt_navigator',
        output='screen',
        parameters=[
            bt_navigator_params_file,
            {'default_nav_to_pose_bt_xml': nav_to_pose_bt_xml},
            {'default_nav_through_poses_bt_xml': nav_through_poses_bt_xml}
        ]
    )

    behavior_server_node = Node(
        package='nav2_behaviors',
        executable='behavior_server',
        name='mapfree_behavior_server',
        output='screen',
        parameters=[behavior_params_file],
        remappings=[('cmd_vel', 'cmd_vel_raw')]
    )

    lifecycle_manager_node = Node(
        package='nav2_lifecycle_manager',
        executable='lifecycle_manager',
        name='mapfree_lifecycle_manager',
        output='screen',
        parameters=[{
            'autostart': False,
            'node_names': [
                'mapfree_planner_server',
                'mapfree_controller_server',
                'mapfree_bt_navigator',
                'mapfree_behavior_server',
                'mapfree_velocity_smoother'
            ]
        }]
    )

    return LaunchDescription([
        static_tf,
        planner_node,
        controller_node,
        velocity_smoother_node,
        bt_navigator_node,
        behavior_server_node,
        lifecycle_manager_node,
    ])


