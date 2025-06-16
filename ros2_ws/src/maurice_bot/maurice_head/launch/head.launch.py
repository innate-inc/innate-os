from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    return LaunchDescription([
        Node(
            package='maurice_head',  # Replace with your package name
            executable='servo_node.py',
            name='servo_node',
            output='screen'
        )
    ])
