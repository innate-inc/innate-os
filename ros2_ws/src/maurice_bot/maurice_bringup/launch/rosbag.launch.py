from launch import LaunchDescription
from launch.actions import ExecuteProcess
from datetime import datetime

def generate_launch_description():
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
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
                '-o', f'/home/jetson1/maurice-prod/ros2_ws/recorder_{timestamp}'
            ],
            output='screen'
        ),
    ])
