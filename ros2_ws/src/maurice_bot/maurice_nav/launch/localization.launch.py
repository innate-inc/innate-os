#!/usr/bin/env python3
import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node, LifecycleNode
from ament_index_python.packages import get_package_share_directory

def generate_launch_description():
    # Use the map file - construct path from environment variable or HOME
    # Note: SLAM Toolbox uses .posegraph files (without extension in path)
    maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
    default_map_path = os.path.join(maurice_root, 'maps', 'home')
    
    # Get the share directory of the maurice_nav package where the SLAM Toolbox config is stored
    maurice_nav_share_dir = get_package_share_directory('maurice_nav')
    localization_params_file = os.path.join(maurice_nav_share_dir, 'config', 'localization_params.yaml')
    
    # Declare launch arguments so that these paths can be overridden if needed
    map_arg = DeclareLaunchArgument(
        'map',
        default_value=default_map_path,
        description='Full path to the map file to load (without .posegraph extension)'
    )
    localization_params_arg = DeclareLaunchArgument(
        'localization_params_file',
        default_value=localization_params_file,
        description='Full path to the SLAM Toolbox localization parameters file'
    )
    
    # Launch SLAM Toolbox in localization mode (replaces map_server + AMCL)
    slam_toolbox_localization_node = LifecycleNode(
        package='slam_toolbox',
        executable='localization_slam_toolbox_node',
        name='slam_toolbox',
        output='screen',
        parameters=[
            LaunchConfiguration('localization_params_file'),
            {'map_file_name': LaunchConfiguration('map')}
        ]
    )
    
    # Lifecycle manager to manage the localization node
    lifecycle_manager_node = Node(
        package='nav2_lifecycle_manager',
        executable='lifecycle_manager',
        name='lifecycle_manager_localization',
        output='screen',
        parameters=[{
            'autostart': True,
            'node_names': ['slam_toolbox']
        }]
    )
    
    return LaunchDescription([
        map_arg,
        localization_params_arg,
        slam_toolbox_localization_node,
        lifecycle_manager_node
    ])

if __name__ == '__main__':
    generate_launch_description()
