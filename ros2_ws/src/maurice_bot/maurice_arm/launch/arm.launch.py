import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node


def generate_launch_description():
    # Get package directory
    maurice_arm_dir = get_package_share_directory('maurice_arm')
    
    # Get the path to the config file
    config_file = os.path.join(maurice_arm_dir, 'config', 'arm_config.yaml')
    
    # Path to planning launch file
    planning_launch_file = os.path.join(maurice_arm_dir, 'launch', 'planning.launch.py')

    # Create the arm node (C++ - includes arm + head servo 7 + MoveIt planning client)
    maurice_arm_node = Node(
        package='maurice_arm',
        executable='arm',
        name='maurice_arm',
        parameters=[config_file],
        output='screen'
    )

    # Create the camera node
    camera_node = Node(
        package='maurice_arm',
        executable='camera_node',
        name='camera',
        output='screen'
    )
    
    # Include the planning launch file (MoveIt move_group)
    planning_launch = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(planning_launch_file)
    )

    return LaunchDescription([
        maurice_arm_node,
        camera_node,
        planning_launch
    ])
