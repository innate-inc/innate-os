from launch import LaunchDescription
from launch.actions import ExecuteProcess

def generate_launch_description():
    return LaunchDescription([
        ExecuteProcess(
            cmd=[
                'ros2', 'bag', 'record',
                '/cmd_vel',
                '/color/image',
                '/image_raw',
                '/maurice_arm/state',
                '/maurice_arm/commands',
                '/chat_in',
                '/chat_out',
                '-o', 'recorder'
            ],
            output='screen'
        ),
    ])
