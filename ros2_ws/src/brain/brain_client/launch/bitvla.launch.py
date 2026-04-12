from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("model_path", default_value="/root/models/bitvla"),
        DeclareLaunchArgument("control_hz", default_value="25"),
        DeclareLaunchArgument("cmd_vel_topic", default_value="/cmd_vel"),

        Node(
            package="brain_client",
            executable="bitvla_node.py",
            name="bitvla_node",
            output="screen",
            parameters=[{
                "model_path": LaunchConfiguration("model_path"),
                "control_hz": LaunchConfiguration("control_hz"),
                "cmd_vel_topic": LaunchConfiguration("cmd_vel_topic"),
            }],
        ),
    ])
