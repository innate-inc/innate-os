#!/usr/bin/env python3
"""
Combined launch file for maurice_nav:
  - Publishes static transforms:
      (a) map -> odom
      (b) odom -> base_footprint
  - Launches the robot_state_publisher to publish the URDF.
  - Runs the odom TF broadcaster.
  - Waits a configurable amount of time before launching Nav2 nodes (planner, controller,
    BT navigator, and lifecycle manager) so that necessary TFs (including the "spin" action server)
    are available.
  
This launch file replaces running the following in separate tmux panes:
  3rd pane: ros2 run tf2_ros static_transform_publisher 0 0 0 0 0 0 map odom
  4th pane: ros2 run robot_state_publisher robot_state_publisher turtlebot3_burger.urdf
  5th pane: python3 /root/maurice-prod/ros2_ws/install/maurice_sim_bringup/lib/maurice_sim_bringup/odom_tf_broadcaster.py 
  6th pane: ros2 run tf2_ros static_transform_publisher 0 0 0 0 0 0 odom base_footprint
  7th pane: (Delayed) Nav2 nodes (planner, controller, BT navigator, lifecycle manager)
"""

import os
from launch import LaunchDescription
from launch.actions import ExecuteProcess, TimerAction
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory


def generate_launch_description():
    # Path to the Nav2 parameter YAML file.
    config_file = os.path.join(
        get_package_share_directory("maurice_nav"),
        "config",
        "nav2_navigation_params.yaml",
    )

    # ---------------------------
    # TF and State Publishers (start immediately)
    # ---------------------------

    # Static transform publisher: map -> odom
    static_tf_map_to_odom = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="static_map_to_odom",
        output="screen",
        arguments=["0", "0", "0", "0", "0", "0", "map", "odom"],
    )

    # Robot state publisher: publish the URDF.
    # (Ensure that 'turtlebot3_burger.urdf' is in your package path or provide its full path.)
    robot_state_pub = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        name="robot_state_publisher",
        output="screen",
        arguments=["turtlebot3_burger.urdf"],
    )

    # Odom TF broadcaster: run your custom odom broadcaster node.
    odom_tf_broadcaster = ExecuteProcess(
        cmd=[
            "python3",
            "/root/maurice-prod/ros2_ws/install/maurice_sim_bringup/lib/maurice_sim_bringup/odom_tf_broadcaster.py",
        ],
        output="screen",
    )

    # Static transform publisher: odom -> base_footprint
    static_tf_odom_to_base_footprint = Node(
        package="tf2_ros",
        executable="static_transform_publisher",
        name="static_odom_to_base_footprint",
        output="screen",
        arguments=["0", "0", "0", "0", "0", "0", "odom", "base_footprint"],
    )

    # ---------------------------
    # Nav2 Nodes (launch with delay)
    # ---------------------------

    # Nav2 Planner Node
    planner_node = Node(
        package="nav2_planner",
        executable="planner_server",
        name="planner_server",
        output="screen",
        parameters=[config_file],
    )

    # Nav2 Controller Node
    controller_node = Node(
        package="nav2_controller",
        executable="controller_server",
        name="controller_server",
        output="screen",
        parameters=[config_file],
    )

    # Nav2 BT Navigator Node
    bt_navigator_node = Node(
        package="nav2_bt_navigator",
        executable="bt_navigator",
        name="bt_navigator",
        output="screen",
        parameters=[config_file],
    )

    # Nav2 Lifecycle Manager Node (manages the above nodes)
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
                    "behavior_server",
                    "bt_navigator",
                ],
            }
        ],
    )

    # Behavior Server (Recovery Node) using nav2_behaviors
    nav2_recovery_node = Node(
        package="nav2_behaviors",
        executable="behavior_server",
        name="behavior_server",
        output="screen",
        parameters=[config_file],
    )

    # Delay the launch of the Nav2 nodes so that all TFs and action servers (like "spin") are ready.
    nav2_delay = TimerAction(
        period=2.0,  # Adjust delay (in seconds) as needed.
        actions=[
            planner_node,
            controller_node,
            nav2_recovery_node,
            bt_navigator_node,
            lifecycle_manager_node,
        ],
    )

    return LaunchDescription(
        [
            static_tf_map_to_odom,
            robot_state_pub,
            odom_tf_broadcaster,
            static_tf_odom_to_base_footprint,
            nav2_delay,
        ]
    )


if __name__ == "__main__":
    generate_launch_description()
