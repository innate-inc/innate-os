import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import LifecycleNode, Node


def generate_launch_description():
    # Launch configurations for sim time.
    use_sim_time = LaunchConfiguration('use_sim_time')

    # Get the parameter file from the package share directory.
    slam_params_file = os.path.join(
        get_package_share_directory("maurice_nav"),
        'config',
        'mapping_params_init.yaml'
    )

    # Declare launch arguments.
    declare_use_sim_time_argument = DeclareLaunchArgument(
        'use_sim_time',
        default_value='true',
        description='Use simulation/Gazebo clock'
    )

    # Configure the asynchronous slam_toolbox node using the parameter file.
    async_slam_toolbox_node = Node(
        parameters=[
            slam_params_file,
            {
                'use_lifecycle_manager': True,
                'use_sim_time': use_sim_time
            }
        ],
        package='slam_toolbox',
        executable='async_slam_toolbox_node',
        name='slam_toolbox',
        namespace='',
        output='screen'
    )
    ld = LaunchDescription()

    ld.add_action(declare_use_sim_time_argument)
    ld.add_action(async_slam_toolbox_node)
    return ld