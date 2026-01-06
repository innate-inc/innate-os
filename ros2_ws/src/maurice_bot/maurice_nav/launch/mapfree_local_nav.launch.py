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

    return LaunchDescription([
        static_tf,
        planner_node,
        controller_node,
    ])


