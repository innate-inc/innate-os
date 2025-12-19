from launch import LaunchDescription
from launch_ros.actions import Node

def generate_launch_description():
    with open("/tmp/head_servo_node.flag3", "w") as f:
        f.write(f"started at {time.time()}\n")


    return LaunchDescription([
        Node(
            package='maurice_head',  # Replace with your package name
            executable='head.py',
            name='head_servo_node',
            output='screen'
        )
    ])
