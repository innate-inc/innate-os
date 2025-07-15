from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    return LaunchDescription([
        Node(
            package='maurice_bringup',
            executable='servo_manager.py',
            name='servo_manager',
            output='screen'
        )
    ])
