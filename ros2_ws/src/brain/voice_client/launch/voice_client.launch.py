#!/usr/bin/env python3
from launch import LaunchDescription
from launch_ros.actions import Node
import os
from pathlib import Path


def generate_launch_description():
    # Load environment variables from .env file if it exists
    # Use environment variable if set, otherwise construct from HOME
    maurice_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
    env_file_path = Path(maurice_root) / ".env"
    if env_file_path.exists():
        with open(env_file_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip()
    
    return LaunchDescription(
        [
            Node(
                package='voice_client',
                executable='voice_client_node.py',
                name='voice_client_node',
                output='screen',
                parameters=[
                    {'mic_device': 'plughw:1,0'},
                    {'sample_rate': 24000},
                    {'channels': 1},
                    {'capture_backend': 'arecord'},
                    {'openai_realtime_model': 'gpt-4o-realtime-preview'},
                    {'openai_realtime_url': 'wss://api.openai.com/v1/realtime'},
                    {'target_sample_rate': 24000},
                    {'target_channels': 1},
                    {'force_resample_downmix': True},
                    {'vad_threshold': 0.5},
                    {'commit_interval_s': 0.0},  # 0 = disabled, server_vad handles segmentation
                ],
            )
        ]
    )


