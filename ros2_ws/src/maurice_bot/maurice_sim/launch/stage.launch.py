#!/usr/bin/env python3
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction, SetLaunchConfiguration, SetEnvironmentVariable
from launch.substitutions import LaunchConfiguration, TextSubstitution
from launch_ros.actions import Node

def generate_launch_description():
    maurice_sim_directory = get_package_share_directory('maurice_sim')

    # Set LD_LIBRARY_PATH environment variable
    set_env_var = SetEnvironmentVariable(
        'LD_LIBRARY_PATH', 
        '/usr/local/lib:' + os.environ.get('LD_LIBRARY_PATH', '')
    )

    # Launch arguments
    world_arg = DeclareLaunchArgument(
        'world',
        default_value=TextSubstitution(text='maurice'),
        description='World file name without .world extension'
    )
    
    stamped_arg = DeclareLaunchArgument(
        'use_stamped_velocity',
        default_value='false',
        description='Publish cmd_vel as TwistStamped instead of Twist'
    )

    def world_file_configuration(context):
        file = os.path.join(
            maurice_sim_directory,
            'worlds',
            context.launch_configurations['world'] + '.world')
        return [SetLaunchConfiguration('world_file', file)]

    world_file_configuration_func = OpaqueFunction(function=world_file_configuration)

    stage_sim = Node(
        package='stage_ros2',
        executable='stage_ros2',
        name='stage_simulator',
        output='screen',
        parameters=[{
            'world_file': LaunchConfiguration('world_file'),
            'use_stamped_velocity': LaunchConfiguration('use_stamped_velocity'),
        }],
        remappings=[
            ('/base_scan', '/scan'),
        ]
    )

    # Static transform publisher from base_link to base_footprint
    static_tf_base = Node(
        package='tf2_ros',
        executable='static_transform_publisher',
        name='base_link_to_base_footprint',
        arguments=['0', '0', '0', '0', '0', '0', 'base_link', 'base_footprint']
    )

    # Static transform publisher from base_link to base_laser (lidar)
    static_tf_lidar = Node(
        package='tf2_ros',
        executable='static_transform_publisher',
        name='lidar_tf',
        arguments=[
            "-0.0764", "0.0", "0.17165",  # Translation: X, Y, Z (in meters)
            "0", "0", "0",                # Rotation: roll, pitch, yaw (in radians)
            "base_link", "base_laser"     # Parent and child frames
        ]
    )

    return LaunchDescription([
        set_env_var,
        world_arg,
        stamped_arg,
        world_file_configuration_func,
        stage_sim,
        static_tf_base,
        static_tf_lidar,
    ])
