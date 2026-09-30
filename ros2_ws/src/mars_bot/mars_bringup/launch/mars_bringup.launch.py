# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node


def generate_launch_description():
    # Get the package share directories
    pkg_dir = get_package_share_directory("mars_bringup")
    sim_pkg_dir = get_package_share_directory("mars_description")

    # Read URDF file for robot_state_publisher
    urdf_file = os.path.join(sim_pkg_dir, "urdf", "mars.urdf")
    with open(urdf_file) as f:
        robot_description = f.read()

    # Robot state publisher node (publishes TF tree from joint_states + URDF)
    robot_state_publisher_node = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        name="robot_state_publisher",
        output="screen",
        parameters=[{"robot_description": robot_description}],
        # Mute the per-link "got segment <link>" startup chatter, and the
        # benign kdl_parser warning about the root link's inertia (KDL drops it,
        # but we keep it in the URDF for the physics sim).
        arguments=[
            "--ros-args",
            "--log-level",
            "robot_state_publisher:=WARN",
            "--log-level",
            "kdl_parser:=ERROR",
        ],
    )

    # Include the bringup_core launch file
    bringup_core_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_dir, "launch", "bringup_core.launch.py"))
    )

    # Include the lidar launch file
    lidar_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(os.path.join(pkg_dir, "launch", "lidar.launch.py"))
    )

    return LaunchDescription([robot_state_publisher_node, bringup_core_launch, lidar_launch])
