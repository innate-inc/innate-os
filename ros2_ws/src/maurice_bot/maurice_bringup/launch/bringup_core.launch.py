from launch import LaunchDescription
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory
import os

def generate_launch_description():
    # Get the package share directory
    pkg_dir = get_package_share_directory('maurice_bringup')
    
    # Path to the config file
    config_file = os.path.join(pkg_dir, 'config', 'robot_config.yaml')
    
    # Create the node
    bringup_node = Node(
        package='maurice_bringup',
        executable='bringup.py',
        name='bringup',
        parameters=[config_file],
        output='screen'
    )
    
    return LaunchDescription([
        bringup_node
    ])
