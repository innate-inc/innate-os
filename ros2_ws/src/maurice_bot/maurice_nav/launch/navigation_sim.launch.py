#!/usr/bin/env python3

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    # Get the package share directory for your package
    # package_name = "maurice_nav" # Unused variable

    config_file = os.path.join(
        get_package_share_directory("maurice_nav"),
        "config",
        "nav2_navigation_params_sim.yaml",
    )

    # Static transform publisher: map -> odom
    static_tf_map_to_odom = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="static_map_to_odom",
        output="screen",
        arguments=["0", "0", "0", "0", "0", "0", "map", "odom"],
    )

    # Dynamic odom -> base_footprint transform from /odom topic
    # MuJoCo sim publishes /odom topic but not TF, so we convert it here
    odom_to_tf_node = Node(
        package="maurice_nav",
        executable="odom_to_tf_node.py",
        name="odom_to_tf_node",
        output="screen",
    )

    # Static transform: base_footprint -> base_link (identity, they're the same)
    static_tf_footprint_to_base = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="static_footprint_to_base_link",
        output="screen",
        arguments=["0", "0", "0", "0", "0", "0", "base_footprint", "base_link"],
    )

    # Create the planner node
    planner_node = Node(
        package="nav2_planner",
        executable="planner_server",
        name="planner_server",
        output="screen",
        parameters=[config_file],
    )

    # Create the controller node
    controller_node = Node(
        package="nav2_controller",
        executable="controller_server",
        name="controller_server",
        output="screen",
        parameters=[config_file],
    )

    # Create the lifecycle manager node to manage all nodes
    # Note: grid_localizer is NOT a lifecycle node, it runs independently
    lifecycle_manager_node = Node(
        package="nav2_lifecycle_manager",
        executable="lifecycle_manager",
        name="lifecycle_manager",
        output="screen",
        parameters=[
            {
                "autostart": True,
                "node_names": [
                    "planner_server",
                    "controller_server",
                    "bt_navigator",
                    "behavior_server",
                ],
            }
        ],
    )

    # Create the BT Navigator node
    bt_navigator_node = Node(
        package="nav2_bt_navigator",
        executable="bt_navigator",
        name="bt_navigator",
        output="screen",
        parameters=[config_file],
    )

    # Create the behavior server node
    behavior_server_node = Node(
        package="nav2_behaviors",
        executable="behavior_server",
        name="behavior_server",
        output="screen",
        parameters=[config_file],
    )

    return LaunchDescription(
        [
            static_tf_map_to_odom,
            odom_to_tf_node,
            static_tf_footprint_to_base,
            planner_node,
            controller_node,
            bt_navigator_node,
            behavior_server_node,
            lifecycle_manager_node,
        ]
    )
