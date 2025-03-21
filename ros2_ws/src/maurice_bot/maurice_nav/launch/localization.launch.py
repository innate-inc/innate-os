#!/usr/bin/env python3
import os

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory

def generate_launch_description():
    # Use the map file located at ~/maurice-prod/maps/map.yaml
    default_map_path = os.path.expanduser('~/maurice-prod/maps/home/home.yaml')
    
    # Get the share directory of the maurice_nav package where the AMCL config is stored
    maurice_nav_share_dir = get_package_share_directory('maurice_nav')
    amcl_params_file = os.path.join(maurice_nav_share_dir, 'config', 'amcl.yaml')
    
    # Declare launch arguments so that these paths can be overridden if needed
    map_arg = DeclareLaunchArgument(
        'map',
        default_value=default_map_path,
        description='Full path to the map file to load'
    )
    amcl_params_arg = DeclareLaunchArgument(
        'amcl_params_file',
        default_value=amcl_params_file,
        description='Full path to the AMCL parameters file'
    )
    
    # Launch the map server node which loads and publishes the map
    map_server_node = Node(
        package='nav2_map_server',
        executable='map_server',
        name='map_server',
        output='screen',
        parameters=[{'yaml_filename': LaunchConfiguration('map')}]
    )
    
    # Launch the AMCL node which uses the map for localization
    amcl_node = Node(
        package='nav2_amcl',
        executable='amcl',
        name='amcl',
        output='screen',
        parameters=[LaunchConfiguration('amcl_params_file')]
    )
    
    return LaunchDescription([
        map_arg,
        amcl_params_arg,
        map_server_node,
        amcl_node
    ])

if __name__ == '__main__':
    generate_launch_description()
