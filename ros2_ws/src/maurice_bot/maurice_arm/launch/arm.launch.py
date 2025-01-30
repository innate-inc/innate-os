from launch import LaunchDescription
from launch_ros.actions import Node
from launch.substitutions import PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare


def generate_launch_description():
    # Get the path to the config file
    config_file = PathJoinSubstitution([
        FindPackageShare('maurice_arm'),
        'config',
        'arm_config.yaml'
    ])

    # Create the node
    maurice_arm_node = Node(
        package='maurice_arm',
        executable='maurice_arm.py',
        name='maurice_arm',
        parameters=[config_file],
        output='screen'
    )

    return LaunchDescription([
        maurice_arm_node
    ])
