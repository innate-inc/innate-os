#!/usr/bin/env python3
"""
Navigation Launch File

Architecture:
┌─────────────────────┐     ┌─────────────────────┐
│   Grid Localizer    │────▶│        AMCL         │
│  (coarse estimate)  │     │  (fine refinement)  │
└─────────────────────┘     └─────────────────────┘
         │                           │
         │ /initialpose              │ continuous
         │ (transient_local QoS)     │ tracking
         ▼                           ▼
    Seeds AMCL's              Publishes map→odom
    particle filter           transform

Grid Localizer runs as a standalone node (not lifecycle-managed) and publishes
initial pose estimates with transient_local durability so AMCL receives them
even if it starts later.
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, TimerAction
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
  
    
    amcl_params_arg = DeclareLaunchArgument(
        'amcl_params_file',
        default_value=amcl_params_file,
        description='Full path to the AMCL parameters file'
    )

    # Create the map server node
    map_server_node = Node(
        package='nav2_map_server',
        executable='map_server',
        name='navigation_map_server',
        output='screen',
        parameters=[]
    )
    
    # Create the AMCL node
    amcl_node = Node(
        package='nav2_amcl',
        executable='amcl',
        name='navigation_amcl',
        output='screen',
        parameters=[LaunchConfiguration('amcl_params_file')],
    )

    # Grid localizer for initial pose estimation
    # Provides coarse localization before AMCL for "kidnapped robot" / unknown initial pose
    # Uses transient_local QoS so /initialpose persists for late-starting AMCL
    grid_localizer_node = Node(
        package='maurice_nav',
        executable='grid_localizer.py',
        name='navigation_grid_localizer',
        output='screen',
        parameters=[{
            'auto_localize': True,
            'auto_localize_timeout': 30.0,
            'max_score_threshold': 0.3,
        }]
    )

    # Create the planner node
    planner_node = Node(
        package='nav2_planner',
        executable='planner_server',
        name='navigation_planner_server',
        output='screen',
        parameters=[planner_params_file, costmap_params_file]
    )

    # Create the controller node
    controller_node = Node(
        package='nav2_controller',
        executable='controller_server',
        name='navigation_controller_server',
        output='screen',
        parameters=[controller_params_file, costmap_params_file],
        remappings=[('cmd_vel', 'cmd_vel_raw')]
    )
    
    # Create the velocity smoother node
    velocity_smoother_node = Node(
        package='nav2_velocity_smoother',
        executable='velocity_smoother',
        name='navigation_velocity_smoother',
        output='screen',
        parameters=[smoother_params_file],
        remappings=[('cmd_vel', '/cmd_vel_raw'),
                    ('cmd_vel_smoothed', '/cmd_vel')]
    )

    # Create the lifecycle manager node to manage all nodes
    lifecycle_manager_node = Node(
        package='nav2_lifecycle_manager',
        executable='navigation_lifecycle_manager',
        name='lifecycle_manager_navigation',
        output='screen',
        parameters=[{
            'autostart': False,
            'bond_timeout': 60.0,
            'attempt_respawn_reconnection': True,
            'bond_respawn_max_duration': 30.0,
            'node_names': [
                'navigation_map_server', 
                'navigation_grid_localizer',
                'navigation_amcl', 
                'navigation_planner_server', 
                'navigation_controller_server', 
                'navigation_bt_navigator', 
                'navigation_behavior_server',
                'navigation_velocity_smoother'
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
        name='navigation_bt_navigator',
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
        name='navigation_behavior_server',
        output='screen',
        parameters=[behavior_params_file],
        arguments=['--ros-args', '--log-level', 'warn'],
        remappings=[
            # send behavior_server's cmd_vel into the smoother's input
            ('cmd_vel', 'cmd_vel_raw'),
        ]
    )

    return LaunchDescription([
        amcl_params_arg,
        # Nav2 lifecycle-managed nodes
        map_server_node,
        grid_localizer_node,
        amcl_node,
        planner_node,
        controller_node,
        velocity_smoother_node,
        bt_navigator_node,
        behavior_server_node,

        lifecycle_manager_node,
    ])
