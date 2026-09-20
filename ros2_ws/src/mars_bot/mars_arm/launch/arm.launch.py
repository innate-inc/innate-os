# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node
from mars_bringup.config_loader import settings_params


def generate_launch_description():
    # Get package directory
    mars_arm_dir = get_package_share_directory("mars_arm")

    # Get the path to the config file
    config_file = os.path.join(mars_arm_dir, "config", "arm_config.yaml")

    # The arm's gravity model reads the masses and centres of mass from the same
    # URDF that robot_state_publisher and the sim do.
    urdf_path = os.path.join(get_package_share_directory("mars_description"), "urdf", "mars.urdf")

    mars_arm_node = Node(
        package="mars_arm",
        executable="arm",
        name="mars_arm",
        parameters=[config_file, {"gravity_compensation.urdf_path": urdf_path}, *settings_params()],
        output="screen",
    )

    return LaunchDescription([mars_arm_node])
