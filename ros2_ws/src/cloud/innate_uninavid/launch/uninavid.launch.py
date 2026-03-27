"""Launch file for the innate_uninavid node."""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description() -> LaunchDescription:
    pkg_dir = get_package_share_directory("innate_uninavid")
    default_params = os.path.join(pkg_dir, "config", "params.yaml")

    params_arg = DeclareLaunchArgument(
        "params_file",
        default_value=default_params,
        description="Full path to the parameter YAML file",
    )

    node = Node(
        package="innate_uninavid",
        executable="uninavid_node",
        name="uninavid_node",
        output="screen",
        parameters=[LaunchConfiguration("params_file")],
        remappings=[
            ("/cmd_vel", "/cmd_vel_scaled"),
        ],
    )

    return LaunchDescription([params_arg, node])
