#!/usr/bin/env python3
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription(
        [
            Node(
                package='voice_client',
                executable='voice_client_node.py',
                name='voice_client_node',
                output='screen',
                parameters=[
                    {'mic_device': 'plughw:3,0'},
                    {'sample_rate': 24000},
                    {'channels': 1},
                    {'capture_backend': 'arecord'},
                    {'openai_realtime_model': 'gpt-4o-realtime-preview'},
                    {'openai_realtime_url': 'wss://api.openai.com/v1/realtime'},
                    {'target_sample_rate': 24000},
                    {'target_channels': 1},
                    {'force_resample_downmix': True},
                    {'vad_threshold': 0.35},
                    {'commit_interval_s': 0.5},
                ],
            )
        ]
    )


