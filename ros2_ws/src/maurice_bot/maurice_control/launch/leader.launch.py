from launch import LaunchDescription
from launch_ros.actions import Node
from ament_index_python.packages import get_package_share_directory
import os

def generate_launch_description():
    # Get the package share directory
    pkg_dir = get_package_share_directory('maurice_control')
    
    # Path to the config file
    config_file = os.path.join(pkg_dir, 'config', 'leader.yaml')
    
    # Create the node
    leader_node = Node(
        package='maurice_control',
        executable='leader.py',
        name='leader_arm',
        parameters=[config_file],
        output='screen'
    )
    
    return LaunchDescription([
        leader_node
    ])
