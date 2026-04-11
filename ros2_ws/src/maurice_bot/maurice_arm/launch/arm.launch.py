import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    # Get package directory
    maurice_arm_dir = get_package_share_directory('maurice_arm')

    # Get the path to the config file
    config_file = os.path.join(maurice_arm_dir, 'config', 'arm_config.yaml')

    # Create the arm node (C++ - arm + head servo 7)
    maurice_arm_node = Node(
        package='maurice_arm',
        executable='arm',
        name='maurice_arm',
        parameters=[config_file],
        output='screen'
    )

    return LaunchDescription([
        maurice_arm_node,
    ])
