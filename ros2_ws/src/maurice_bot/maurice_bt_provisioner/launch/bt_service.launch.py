from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription(
        [
            Node(
                package="maurice_bt_provisioner",
                executable="simple_bt_service.py",
                name="bt_service",
                output="screen",
            ),
        ]
    )
