#!/usr/bin/env python3

from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    return LaunchDescription([
        Node(
            package='maurice_nav',
            executable='mode_manager.py',
            name='mode_manager',
            output='screen',
            parameters=[]
        )
    ]) 