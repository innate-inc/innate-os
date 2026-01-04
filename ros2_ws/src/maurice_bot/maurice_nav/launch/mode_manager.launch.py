#!/usr/bin/env python3

import os

from ament_index_python.packages import get_package_share_directory

from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node

def generate_launch_description():
    maurice_nav_launch_dir = os.path.join(
        get_package_share_directory('maurice_nav'), 'launch')
    
    navigation_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource([maurice_nav_launch_dir, '/navigation.launch.py'])
    )
    
    mapfree_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource([maurice_nav_launch_dir, '/mapfree_local_nav.launch.py'])
    )
    
    mapping_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource([maurice_nav_launch_dir, '/mapping.launch.py'])
    )
    
    return LaunchDescription([
        navigation_launch,
        mapfree_launch,
        mapping_launch,
        Node(
            package='maurice_nav',
            executable='mode_manager.py',
            name='mode_manager',
            output='screen',
            parameters=[]
        )
    ]) 