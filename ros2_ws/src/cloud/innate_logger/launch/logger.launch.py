"""Launch file for the innate_logger node."""

import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription(
        [
            Node(
                package="innate_logger",
                executable="logger_node",
                name="logger_node",
                output="screen",
                parameters=[],
            ),
        ]
    )
