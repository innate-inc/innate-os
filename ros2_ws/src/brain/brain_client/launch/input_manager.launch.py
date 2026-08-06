# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, SetEnvironmentVariable
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from mars_bringup.config_loader import get_env, load_env_file, settings_params

from brain_client.common.logging import get_logging_env_vars


def generate_launch_description():
    # Load runtime secrets and non-secret OS config.
    load_env_file()

    # Get logging environment variables
    env_vars = get_logging_env_vars()

    # --- Proxy service configuration ---
    # Credentials come from env vars (INNATE_PROXY_URL, INNATE_SERVICE_KEY)
    # These are service configs that can be overridden at launch
    openai_realtime_model_arg = DeclareLaunchArgument(
        "openai_realtime_model",
        default_value="gpt-4o-realtime-preview",
        description="OpenAI Realtime model for STT",
    )
    openai_realtime_url_arg = DeclareLaunchArgument(
        "openai_realtime_url",
        default_value="wss://api.openai.com/v1/realtime",
        description="OpenAI Realtime WebSocket URL",
    )
    openai_transcribe_model_arg = DeclareLaunchArgument(
        "openai_transcribe_model",
        default_value="gpt-4o-mini-transcribe",
        description="OpenAI transcription model",
    )
    cartesia_voice_id_arg = DeclareLaunchArgument(
        "cartesia_voice_id",
        # Same env default as brain_client.launch.py, so .env CARTESIA_VOICE_ID sets both speech paths.
        default_value=get_env("CARTESIA_VOICE_ID", "9fdaae0b-f885-4813-b589-3c07cf9d5fea"),
        description="Cartesia Alfred voice id",
    )

    # Barge-in detection runs many tiny matrix ops per audio frame. Multi-threaded
    # BLAS spends far more on thread hand-off than on the arithmetic (measured
    # 6.0ms vs 0.47ms per call on the Orin) and grabs every core while doing it.
    # This node has no large linear algebra, so pin BLAS to one thread.
    blas_single_thread = [
        SetEnvironmentVariable(name=var, value="1")
        for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
    ]

    return LaunchDescription(
        env_vars
        + blas_single_thread
        + [
            openai_realtime_model_arg,
            openai_realtime_url_arg,
            openai_transcribe_model_arg,
            cartesia_voice_id_arg,
            Node(
                package="brain_client",
                executable="input_manager.py",
                name="input_manager_node",
                output="screen",
                parameters=[
                    {
                        "openai_realtime_model": LaunchConfiguration("openai_realtime_model"),
                        "openai_realtime_url": LaunchConfiguration("openai_realtime_url"),
                        "openai_transcribe_model": LaunchConfiguration("openai_transcribe_model"),
                        "cartesia_voice_id": LaunchConfiguration("cartesia_voice_id"),
                    },
                    *settings_params(),
                ],
            ),
        ]
    )
