# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription(
        [
            Node(
                package="mars_cam",
                executable="webrtc_streamer_node",
                name="webrtc_streamer",
                output="screen",
                parameters=[
                    {
                        "cameras": ["main", "arm", "main_rect"],
                        "live_main_camera_topic": "/mars/main_camera/left/image_raw",
                        "live_arm_camera_topic": "/mars/arm/image_raw",
                        "live_main_rect_camera_topic": "/mars/main_camera/left/image_rect_color",
                        "replay_main_camera_topic": "/brain/recorder/replay/main_camera/left/image_raw",
                        "replay_arm_camera_topic": "/brain/recorder/replay/arm_camera/image_raw",
                        "replay_main_rect_camera_topic": "/brain/recorder/replay/main_camera/left/image_raw",
                        "main_rect_fps": 8,
                        # Mic to teleoperator. Address the Arducam by stable card name rather than
                        # leaving it empty (ALSA "default" can resolve to the wrong/non-capture card).
                        "enable_audio": True,
                        "audio_source_element": "alsasrc",
                        "audio_capture_device": "sysdefault:CARD=Light",
                    }
                ],
            )
        ]
    )
