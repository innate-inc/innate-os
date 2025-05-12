#!/usr/bin/env python3
import os
from launch import LaunchDescription
from launch_ros.actions import Node
from launch.actions import ExecuteProcess
from ament_index_python.packages import get_package_share_directory

def generate_launch_description():
    # Locate the package share directory and the YAML configuration file.
    pkg_share = get_package_share_directory('manipulation')
    config_file = os.path.join(pkg_share, 'config', 'recorder.yaml')
    
    # Get the URDF file path
    urdf_file = os.path.join(get_package_share_directory('maurice_sim'), 'urdf', 'maurice.urdf')
    
    # Create a temporary RViz config file
    rviz_config = os.path.join(pkg_share, 'config', 'visualizer.rviz')
    
    # Define the robot state publisher node
    robot_state_publisher = Node(
        package='robot_state_publisher',
        executable='robot_state_publisher',
        name='robot_state_publisher',
        output='screen',
        parameters=[{'robot_description': open(urdf_file).read()}]
    )

    # Define the visualizer node with its parameters.
    visualizer_node = Node(
        package='manipulation',
        executable='visualizer.py',
        name='visualizer_node',
        output='screen',
        parameters=[config_file]
    )

    # Define the RViz2 node
    rviz_node = Node(
        package='rviz2',
        executable='rviz2',
        name='rviz2',
        output='screen',
        arguments=['-d', rviz_config]
    )

    return LaunchDescription([
        robot_state_publisher,
        visualizer_node,
        rviz_node
    ])
