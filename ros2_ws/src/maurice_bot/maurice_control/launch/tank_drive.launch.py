#!/usr/bin/env python3

from launch import LaunchDescription
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory
import os


def generate_launch_description():
    # Get the package share directory
    pkg_share = get_package_share_directory('maurice_control')
    config_file = os.path.join(pkg_share, 'config', 'motion_control.yaml')

    return LaunchDescription([
        Node(
            package='maurice_control',
            executable='tank_drive.py',
            name='tank_drive_controller',
            output='screen',
            parameters=[
                config_file,
                {
                    'max_speed': 0.6,           # m/s - adjust as needed
                    'max_angular_speed': 1.5,   # rad/s - adjust as needed
                    'deadzone': 0.15,
                    'update_rate': 50.0,
                    'left_axis': 1,             # Left stick Y
                    'right_axis': 3,            # Right stick Y
                    'invert_left': True,
                    'invert_right': True,
                    'joystick_index': 0,
                }
            ],
        ),
    ])

