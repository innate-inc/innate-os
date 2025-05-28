#!/usr/bin/env python3

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import ExecuteProcess
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

    # Robot state publisher: publish the URDF.
    # (Ensure that 'turtlebot3_burger.urdf' is in your package path
    # or provide its full path.)
    robot_state_pub = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        name="robot_state_publisher",
        output="screen",
        arguments=["turtlebot3_burger.urdf"],
    )

    # Odom TF broadcaster: run your custom odom broadcaster node.
    odom_broadcaster_script = (
        "/root/maurice-prod/ros2_ws/install/maurice_sim_bringup/lib/"
        "maurice_sim_bringup/odom_tf_broadcaster.py"
    )
    odom_tf_broadcaster = ExecuteProcess(
        cmd=["python3", odom_broadcaster_script],
        output="screen",
    )

    # Static transform publisher: odom -> base_footprint
    # static_tf_odom_to_base_footprint = Node(
    #     package="tf2_ros",
    #     executable="static_transform_publisher",
    #     name="static_odom_to_base_footprint",
    #     output="screen",
    #     arguments=["0", "0", "0", "0", "0", "0", "odom", "base_link"],
    # )

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
            robot_state_pub,
            odom_tf_broadcaster,
            # static_tf_odom_to_base_footprint,
            planner_node,
            controller_node,
            bt_navigator_node,
            behavior_server_node,
            lifecycle_manager_node,
        ]
    )
