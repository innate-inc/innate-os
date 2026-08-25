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
                        "live_main_camera_topic": "/mars/main_camera/left/image_raw",
                        "live_arm_camera_topic": "/mars/arm/image_raw",
                        "replay_main_camera_topic": "/brain/recorder/replay/main_camera/left/image_raw",
                        "replay_arm_camera_topic": "/brain/recorder/replay/arm_camera/image_raw",
                        # Mic to teleoperator. Address the Arducam by stable card name rather than
                        # leaving it empty (ALSA "default" can resolve to the wrong/non-capture card).
                        "enable_audio": True,
                        "audio_source_element": "alsasrc",
                        "audio_capture_device": "sysdefault:CARD=Light",
                        "enable_talkback": True,  # operator push-to-talk out the robot speaker
                    }
                ],
            )
        ]
    )
