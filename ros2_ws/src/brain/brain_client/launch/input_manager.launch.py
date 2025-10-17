from launch import LaunchDescription
from launch_ros.actions import Node
from brain_client.logging_config import get_logging_env_vars


def generate_launch_description():
    # Get logging environment variables
    env_vars = get_logging_env_vars()

    return LaunchDescription(
        env_vars
        + [
            Node(
                package="brain_client",
                executable="input_manager_node.py",
                name="input_manager_node",
                output="screen",
            ),
        ]
    )

