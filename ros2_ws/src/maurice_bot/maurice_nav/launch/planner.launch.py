#!/usr/bin/env python3

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

def generate_launch_description():
    # Get the package share directory for your package
    package_name = 'maurice_nav'
    share_dir = get_package_share_directory(package_name)
    # We no longer need nav2_bt_navigator share dir for this file

    # Define the paths to your YAML files
    planner_params_file = os.path.join(share_dir, 'config', 'planner.yaml')
    costmap_params_file = os.path.join(share_dir, 'config', 'costmap.yaml') # Planner might need costmap params
    amcl_params_file = os.path.join(share_dir, 'config', 'amcl.yaml')
    bt_navigator_params_file = os.path.join(share_dir, 'config', 'bt_navigator.yaml')
    behavior_params_file = os.path.join(share_dir, 'config', 'behavior.yaml')

    # Define the path to the compute_path_to_pose BT XML within our package
    compute_path_bt_xml_path = os.path.join(
        share_dir, # Use our package's share dir
        'config',
        'compute_path_to_pose.xml')
    
    # Define paths to standard BT XMLs
    nav_to_pose_bt_xml = os.path.join(share_dir, 'config', 'nav_to_pose.xml')
    nav_through_poses_bt_xml = os.path.join(share_dir, 'config', 'nav_through_poses.xml')

    # Define the default map path using environment variable or HOME
    maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
    default_map_path = os.path.join(maurice_root, 'maps', 'home.yaml')

    # Declare launch arguments
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

    # We don't need the bt_xml_arg anymore if we hardcode the path below

    # Create the map server node
    map_server_node = Node(
        package='nav2_map_server',
        executable='map_server',
        name='map_server',
        output='screen',
        parameters=[{'yaml_filename': LaunchConfiguration('map')}]
    )

    # Create the AMCL node
    amcl_node = Node(
        package='nav2_amcl',
        executable='amcl',
        name='amcl',
        output='screen',
        parameters=[LaunchConfiguration('amcl_params_file')]
    )

    # Create the planner node
    planner_node = Node(
        package='nav2_planner',
        executable='planner_server',
        name='planner_server',
        output='screen',
        parameters=[planner_params_file, costmap_params_file]
    )

    # Create the BT Navigator node
    bt_navigator_node = Node(
        package='nav2_bt_navigator',
        executable='bt_navigator',
        name='bt_navigator',
        output='screen',
        parameters=[
            bt_navigator_params_file,
            # Point to the BT XML in our package's config directory
            {'default_nav_to_pose_bt_xml': compute_path_bt_xml_path},
            {'default_nav_through_poses_bt_xml': nav_through_poses_bt_xml}
        ]
    )

    # Create the behavior server node
    behavior_server_node = Node(
        package='nav2_behaviors',
        executable='behavior_server',
        name='behavior_server',
        output='screen',
        parameters=[behavior_params_file]
    )

    # Create the lifecycle manager node to manage the included nodes
    lifecycle_manager_node = Node(
        package='nav2_lifecycle_manager',
        executable='lifecycle_manager',
        name='lifecycle_manager',
        output='screen',
        parameters=[{
            'autostart': True,
            'node_names': ['map_server', 'amcl', 'planner_server', 'bt_navigator', 'behavior_server']
        }]
    )

    return LaunchDescription([
        map_arg,
        amcl_params_arg,
        # Removed bt_xml_arg
        map_server_node,
        amcl_node,
        planner_node,
        bt_navigator_node,
        behavior_server_node,
        lifecycle_manager_node
    ])
