#!/usr/bin/env python3
import os
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    root = os.environ.get('INNATE_OS_ROOT', os.path.expanduser('~/innate-os'))
    default_map = os.path.join(root, 'maps', 'office_theo.yaml')

    return LaunchDescription([
        DeclareLaunchArgument('map_yaml_path', default_value=default_map),
        DeclareLaunchArgument('scan_topic', default_value='/scan_fast'),
        DeclareLaunchArgument('sample_distance', default_value='0.15'),
        DeclareLaunchArgument('auto_localize', default_value='true'),
        
        Node(
            package='maurice_nav',
            executable='grid_localizer.py',
            name='grid_localizer',
            output='screen',
            parameters=[{
                'map_yaml_path': LaunchConfiguration('map_yaml_path'),
                'scan_topic': LaunchConfiguration('scan_topic'),
                'sample_distance': LaunchConfiguration('sample_distance'),
                'auto_localize': LaunchConfiguration('auto_localize'),
            }]
        ),
    ])





