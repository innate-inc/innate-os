#!/usr/bin/env python3
"""
Launch file for maurice_nav:
Starts the Nav2 planner, controller, BT navigator, recoveries, and lifecycle manager.
"""

import os
from launch import LaunchDescription
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    # Path to the YAML configuration file
    config_file = os.path.join(
        get_package_share_directory("maurice_nav"),
        "config",
        "nav2_navigation_params.yaml",
    )

    # Define Nav2 nodes
    planner_node = Node(
        package="nav2_planner",
        executable="planner_server",
        name="planner_server",
        output="screen",
        parameters=[config_file],
    )

    controller_node = Node(
        package="nav2_controller",
        executable="controller_server",
        name="controller_server",
        output="screen",
        parameters=[config_file],
    )

    bt_navigator_node = Node(
        package="nav2_bt_navigator",
        executable="bt_navigator",
        name="bt_navigator",
        output="screen",
        parameters=[config_file],
    )

    lifecycle_manager_node = Node(
        package="nav2_lifecycle_manager",
        executable="lifecycle_manager",
        name="lifecycle_manager_navigation",
        output="screen",
        parameters=[
            {
                "autostart": True,
                "node_names": [
                    "planner_server",
                    "controller_server",
                    "bt_navigator",
                ],
            }
        ],
    )

    return LaunchDescription(
        [
            planner_node,
            controller_node,
            bt_navigator_node,
            lifecycle_manager_node,
        ]
    )
