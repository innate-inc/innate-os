from launch import LaunchDescription
from launch.actions import ExecuteProcess
from datetime import datetime
import os

def generate_launch_description():
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # Use environment variable if set, otherwise construct from HOME
    maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'maurice-prod'))
    recordings_dir = os.path.join(maurice_root, 'recordings')
    
    return LaunchDescription([
        ExecuteProcess(
            cmd=[
                'ros2', 'bag', 'record',
                '/cmd_vel',
                '/color/image/compressed',
                '/image_raw/compressed',
                '/maurice_arm/state',
                '/maurice_arm/commands',
                '/chat_in',
                '/chat_out',
                '-o', f'{recordings_dir}/recorder_{timestamp}'
            ],
            output='screen'
        ),
    ])
