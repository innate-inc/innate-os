from launch import LaunchDescription
from launch_ros.actions import Node
from launch.substitutions import PathJoinSubstitution
from launch_ros.substitutions import FindPackageShare
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration


def generate_launch_description():
    # Get the path to the config file
    config_file = PathJoinSubstitution([
        FindPackageShare('maurice_arm'),
        'config',
        'arm_config.yaml'
    ])

    # Create the servo manager node first
    servo_manager_node = Node(
        package='maurice_arm',
        executable='servo_manager.py',
        name='servo_manager',
        output='screen'
    )

    # Create the arm node that will wait for servo manager
    maurice_arm_node = Node(
        package='maurice_arm',
        executable='arm.py',
        name='arm',
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

    # Create the arm utils node
    arm_utils_node = Node(
        package='maurice_arm',
        executable='arm_utils.py',
        name='arm_utils',
        output='screen'
    )

    return LaunchDescription([
        servo_manager_node,
        maurice_arm_node,
        camera_node,
        arm_utils_node
    ])
