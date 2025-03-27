#!/usr/bin/env python3
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, SetEnvironmentVariable
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

def generate_launch_description():
    package_name = 'maurice_sim'
    pkg_share = get_package_share_directory(package_name)
    model_path = os.path.join(pkg_share, 'mjcf', 'maurice.mjcf')
    
    # Add path to the cameras config file
    cameras_config = os.path.join(pkg_share, 'config', 'cameras.yaml')

    return LaunchDescription([
        # Set NVIDIA GPU offloading environment variables
        SetEnvironmentVariable(name='__NV_PRIME_RENDER_OFFLOAD', value='1'),
        SetEnvironmentVariable(name='__GLX_VENDOR_LIBRARY_NAME', value='nvidia'),
        
        # Declare a launch argument for the model path (default provided)
        DeclareLaunchArgument(
            'model_path',
            default_value=model_path,
            description='Absolute path to the MJCF file.'
        ),
        # Launch the node and pass the model_path parameter
        Node(
            package=package_name,
            executable='sim.py',  # Updated to the correct node executable name
            name='maurice_sim_node',
            output='screen',
            parameters=[
                {'model_path': LaunchConfiguration('model_path')},
                cameras_config  # Add the cameras config file
            ]
        )
    ])

if __name__ == '__main__':
    generate_launch_description()
