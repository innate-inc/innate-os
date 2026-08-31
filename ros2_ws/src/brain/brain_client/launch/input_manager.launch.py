# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue
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
    stt_backend_arg = DeclareLaunchArgument(
        "stt_backend",
        default_value="elevenlabs",
        description="STT backend: elevenlabs (realtime, default) | elevenlabs_batch | gemini (batch)",
    )
    stt_language_arg = DeclareLaunchArgument(
        "stt_language",
        default_value="en",
        description="Transcription language code",
    )
    stt_vad_threshold_arg = DeclareLaunchArgument(
        "stt_vad_threshold",
        default_value="0.2",
        description="Silero speech probability that counts as speech (lower = more sensitive)",
    )
    stt_vad_silence_secs_arg = DeclareLaunchArgument(
        "stt_vad_silence_secs",
        default_value="0.5",
        description="Silence that closes an utterance, in seconds (every backend)",
    )
    stt_agc_max_db_arg = DeclareLaunchArgument(
        "stt_agc_max_db",
        default_value="24.0",
        description="Software mic gain ceiling in dB (slow AGC toward -6 dBFS peak); 0 disables",
    )
    stt_filter_background_audio_arg = DeclareLaunchArgument(
        "stt_filter_background_audio",
        default_value="true",
        description="Scribe realtime: server-side gate against nearby conversations and ambient noise",
    )
    stt_energy_threshold_arg = DeclareLaunchArgument(
        "stt_energy_threshold",
        default_value="0.01",
        description="Normalized RMS (0-1) above which a mic chunk counts as speech (energy engine)",
    )
    stt_vad_engine_arg = DeclareLaunchArgument(
        "stt_vad_engine",
        default_value="silero",
        description="Local voice detector: silero (neural) | energy (RMS threshold)",
    )
    stt_commit_strategy_arg = DeclareLaunchArgument(
        "stt_commit_strategy",
        default_value="vad",
        description="Who cuts utterances on Scribe realtime: vad (Scribe's own) | manual (local endpointer)",
    )
    stt_realtime_vad_threshold_arg = DeclareLaunchArgument(
        "stt_realtime_vad_threshold",
        default_value="0.4",
        description="Scribe VAD speech probability (commit_strategy=vad only)",
    )
    stt_realtime_vad_silence_secs_arg = DeclareLaunchArgument(
        "stt_realtime_vad_silence_secs",
        default_value="0.5",
        description="Scribe VAD silence that closes an utterance, 0.3-3.0 s (commit_strategy=vad only)",
    )
    stt_realtime_min_speech_ms_arg = DeclareLaunchArgument(
        "stt_realtime_min_speech_ms",
        default_value="100",
        description="Scribe VAD shortest run of speech that opens an utterance (commit_strategy=vad only)",
    )
    stt_realtime_min_silence_ms_arg = DeclareLaunchArgument(
        "stt_realtime_min_silence_ms",
        default_value="100",
        description="Scribe VAD shortest gap that counts as silence (commit_strategy=vad only)",
    )
    elevenlabs_batch_stt_model_arg = DeclareLaunchArgument(
        "elevenlabs_batch_stt_model",
        default_value="scribe_v2",
        description="ElevenLabs Scribe model for batch utterance transcription",
    )
    gemini_stt_model_arg = DeclareLaunchArgument(
        "gemini_stt_model",
        default_value="gemini-3.6-flash",
        description="Gemini model for batch utterance transcription",
    )
    elevenlabs_stt_model_arg = DeclareLaunchArgument(
        "elevenlabs_stt_model",
        default_value="scribe_v2_realtime",
        description="ElevenLabs Scribe realtime model",
    )
    cartesia_voice_id_arg = DeclareLaunchArgument(
        "cartesia_voice_id",
        # Same env default as brain_client.launch.py, so .env CARTESIA_VOICE_ID sets both speech paths.
        default_value=get_env("CARTESIA_VOICE_ID", "9fdaae0b-f885-4813-b589-3c07cf9d5fea"),
        description="Cartesia Alfred voice id",
    )

    return LaunchDescription(
        env_vars
        + [
            stt_backend_arg,
            stt_language_arg,
            stt_vad_threshold_arg,
            stt_vad_silence_secs_arg,
            stt_agc_max_db_arg,
            stt_filter_background_audio_arg,
            stt_energy_threshold_arg,
            stt_vad_engine_arg,
            stt_commit_strategy_arg,
            stt_realtime_vad_threshold_arg,
            stt_realtime_vad_silence_secs_arg,
            stt_realtime_min_speech_ms_arg,
            stt_realtime_min_silence_ms_arg,
            elevenlabs_batch_stt_model_arg,
            gemini_stt_model_arg,
            elevenlabs_stt_model_arg,
            cartesia_voice_id_arg,
            Node(
                package="brain_client",
                executable="input_manager.py",
                name="input_manager_node",
                output="screen",
                parameters=[
                    {
                        "stt_backend": LaunchConfiguration("stt_backend"),
                        "stt_language": LaunchConfiguration("stt_language"),
                        # Substitutions resolve to strings; the node declares these
                        # as doubles, so coerce or the node rejects the parameter.
                        "stt_vad_threshold": ParameterValue(LaunchConfiguration("stt_vad_threshold"), value_type=float),
                        "stt_vad_silence_secs": ParameterValue(
                            LaunchConfiguration("stt_vad_silence_secs"), value_type=float
                        ),
                        "stt_agc_max_db": ParameterValue(LaunchConfiguration("stt_agc_max_db"), value_type=float),
                        "stt_filter_background_audio": ParameterValue(
                            LaunchConfiguration("stt_filter_background_audio"), value_type=bool
                        ),
                        "stt_energy_threshold": ParameterValue(
                            LaunchConfiguration("stt_energy_threshold"), value_type=float
                        ),
                        "stt_vad_engine": LaunchConfiguration("stt_vad_engine"),
                        "stt_commit_strategy": LaunchConfiguration("stt_commit_strategy"),
                        "stt_realtime_vad_threshold": ParameterValue(
                            LaunchConfiguration("stt_realtime_vad_threshold"), value_type=float
                        ),
                        "stt_realtime_vad_silence_secs": ParameterValue(
                            LaunchConfiguration("stt_realtime_vad_silence_secs"), value_type=float
                        ),
                        "stt_realtime_min_speech_ms": ParameterValue(
                            LaunchConfiguration("stt_realtime_min_speech_ms"), value_type=int
                        ),
                        "stt_realtime_min_silence_ms": ParameterValue(
                            LaunchConfiguration("stt_realtime_min_silence_ms"), value_type=int
                        ),
                        "elevenlabs_batch_stt_model": LaunchConfiguration("elevenlabs_batch_stt_model"),
                        "gemini_stt_model": LaunchConfiguration("gemini_stt_model"),
                        "elevenlabs_stt_model": LaunchConfiguration("elevenlabs_stt_model"),
                        "cartesia_voice_id": LaunchConfiguration("cartesia_voice_id"),
                    },
                    *settings_params(),
                ],
            ),
        ]
    )
