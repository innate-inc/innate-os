# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node
from mars_bringup.config_loader import innate_os_root, settings_params


def generate_launch_description():
    data_directory = str(innate_os_root() / "data")
    motor_sound_config = os.path.join(get_package_share_directory("mars_control"), "config", "motor_sound.yaml")

    # Default hardware revision for new robots
    default_hardware_revision = "R6"

    app_node = Node(
        package="mars_control",
        executable="app.cpp",
        name="mars_app",
        output="screen",
        parameters=[
            {
                "data_directory": data_directory,
                "default_hardware_revision": default_hardware_revision,
            },
            *settings_params(),
        ],
    )

    # The container has no speaker. Stream the same configured synth as PCM;
    # the simulator webapp is the audio device.
    motor_sound_node = Node(
        package="mars_control",
        executable="motor_sound.py",
        name="motor_sound",
        parameters=[motor_sound_config, *settings_params(), {"motor_sound.browser_audio_topic": "/motor_sound/audio"}],
        output="screen",
    )

    return LaunchDescription([app_node, motor_sound_node])
