from launch import LaunchDescription
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory
import os

def generate_launch_description():
    # Get the package share directory
    pkg_dir = get_package_share_directory('maurice_bringup')
    
    # Path to the config file
    config_file = os.path.join(pkg_dir, 'config', 'robot_config.yaml')
    
    # Create the nodes
    bringup_node = Node(
        package='maurice_bringup',
        executable='bringup.py',
        name='bringup',
        parameters=[config_file],
        output='screen'
    )
    
    # Add static transform publisher for base_link to base_footprint
    base_to_footprint_publisher = Node(
        package='tf2_ros',
        executable='static_transform_publisher',
        name='base_to_footprint_publisher',
        arguments=['0', '0', '0', '0', '0', '0', 'base_link', 'base_footprint']
    )
    
    return LaunchDescription([
        bringup_node,
        base_to_footprint_publisher
    ])
