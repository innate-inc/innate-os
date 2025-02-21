#!/usr/bin/env python3
import os
from launch import LaunchDescription
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory

def generate_launch_description():
    # Locate the package share directory and the YAML configuration file.
    pkg_share = get_package_share_directory('manipulation')
    config_file = os.path.join(pkg_share, 'config', 'recorder.yaml')

    # Define the recorder node with its parameters.
    recorder_node = Node(
        package='manipulation',
        executable='recorder.py',
        name='recorder_node',
        output='screen',
        parameters=[config_file]
    )

    return LaunchDescription([
        recorder_node
    ])
