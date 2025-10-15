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

    # Define the paths to your YAML files
    planner_params_file = os.path.join(share_dir, 'config', 'planner.yaml')
    controller_params_file = os.path.join(share_dir, 'config', 'controller.yaml')
    costmap_params_file = os.path.join(share_dir, 'config', 'costmap.yaml')
    amcl_params_file = os.path.join(share_dir, 'config', 'amcl.yaml')
    bt_navigator_params_file = os.path.join(share_dir, 'config', 'bt_navigator.yaml')
    behavior_params_file = os.path.join(share_dir, 'config', 'behavior.yaml')
    smoother_params_file = os.path.join(share_dir, 'config', 'velocity_smoother.yaml')

    # Use the map file - construct path from environment variable or HOME
    maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
    default_map_path = os.path.join(maurice_root, 'maps', 'home.yaml')
    
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

    # Create the controller node
    controller_node = Node(
        package='nav2_controller',
        executable='controller_server',
        name='controller_server',
        output='screen',
        parameters=[controller_params_file, costmap_params_file],
        remappings=[('cmd_vel', 'cmd_vel_raw')]
    )
    
    # Create the velocity smoother node
    velocity_smoother_node = Node(
        package='nav2_velocity_smoother',
        executable='velocity_smoother',
        name='velocity_smoother',
        output='screen',
        parameters=[smoother_params_file],
        remappings=[('cmd_vel', '/cmd_vel_raw'),
                    ('cmd_vel_smoothed', '/cmd_vel')]
    )

    # Create the lifecycle manager node to manage all nodes
    lifecycle_manager_node = Node(
        package='nav2_lifecycle_manager',
        executable='lifecycle_manager',
        name='lifecycle_manager',
        output='screen',
        parameters=[{
            'autostart': True,
            'node_names': [
                'map_server', 
                'amcl', 
                'planner_server', 
                'controller_server', 
                'bt_navigator', 
                'behavior_server',
                'velocity_smoother'
                ]
        }]
    )

    # Create the BT Navigator node
    # Override BT XML paths with package-relative paths
    nav_to_pose_bt_xml = os.path.join(share_dir, 'config', 'nav_to_pose.xml')
    nav_through_poses_bt_xml = os.path.join(share_dir, 'config', 'nav_through_poses.xml')
    
    bt_navigator_node = Node(
        package='nav2_bt_navigator',
        executable='bt_navigator',
        name='bt_navigator',
        output='screen',
        parameters=[
            bt_navigator_params_file,
            {'default_nav_to_pose_bt_xml': nav_to_pose_bt_xml},
            {'default_nav_through_poses_bt_xml': nav_through_poses_bt_xml}
        ]
    )
    
    # Create the behavior server node
    behavior_server_node = Node(
        package='nav2_behaviors',
        executable='behavior_server',
        name='behavior_server',
        output='screen',
        parameters=[behavior_params_file],
        arguments=['--ros-args', '--log-level', 'warn'],
        remappings=[
            # send behavior_server's cmd_vel into the smoother's input
            ('cmd_vel', 'cmd_vel_raw'),
        ]
    )

    return LaunchDescription([
        map_arg,
        amcl_params_arg,
        map_server_node,
        amcl_node,
        planner_node,
        controller_node,
        velocity_smoother_node,
        bt_navigator_node,
        behavior_server_node,
        lifecycle_manager_node
    ])
