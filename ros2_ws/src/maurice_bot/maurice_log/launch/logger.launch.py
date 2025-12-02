from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    # Declare launch argument for telemetry URL
    telemetry_url_arg = DeclareLaunchArgument(
        'telemetry_url',
        default_value='https://logs.innate.bot',
        description='URL of the telemetry logging service'
    )

    # Logger node
    logger_node = Node(
        package='maurice_log',
        executable='logger_node.py',
        name='logger_node',
        output='screen',
        parameters=[{
            'telemetry_url': LaunchConfiguration('telemetry_url')
        }]
    )

    return LaunchDescription([
        telemetry_url_arg,
        logger_node,
    ])
