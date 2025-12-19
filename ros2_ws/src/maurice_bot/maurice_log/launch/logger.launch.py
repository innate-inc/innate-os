import os
from pathlib import Path
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def _load_env():
    """Load .env file from innate-os root."""
    innate_root = os.environ.get(
        'INNATE_OS_ROOT', 
        os.path.join(os.path.expanduser('~'), 'innate-os')
    )
    env_path = Path(innate_root) / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip()


def generate_launch_description():
    # Load environment variables from .env file
    _load_env()
    
    # Declare launch argument for telemetry URL
    telemetry_url_arg = DeclareLaunchArgument(
        'telemetry_url',
        default_value=os.environ.get('TELEMETRY_URL', 'https://logs.innate.bot'),
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
