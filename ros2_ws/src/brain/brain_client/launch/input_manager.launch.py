from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from brain_client.logging_config import get_logging_env_vars
import os
from pathlib import Path


def generate_launch_description():
    # Load environment variables from .env file if it exists
    innate_os_root = os.environ.get('INNATE_OS_ROOT', os.path.join(os.path.expanduser('~'), 'innate-os'))
    env_file_path = Path(innate_os_root) / ".env"
    if env_file_path.exists():
        with open(env_file_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    os.environ[key.strip()] = value.strip()
    
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
        default_value="f786b574-daa5-4673-aa0c-cbe3e8534c02",
        description="Cartesia voice ID for TTS (Katie - Friendly Fixer)",
    )

    return LaunchDescription(
        env_vars
        + [
            openai_realtime_model_arg,
            openai_realtime_url_arg,
            openai_transcribe_model_arg,
            cartesia_voice_id_arg,
            Node(
                package="brain_client",
                executable="input_manager_node.py",
                name="input_manager_node",
                output="screen",
                parameters=[
                    {
                        "openai_realtime_model": LaunchConfiguration("openai_realtime_model"),
                        "openai_realtime_url": LaunchConfiguration("openai_realtime_url"),
                        "openai_transcribe_model": LaunchConfiguration("openai_transcribe_model"),
                        "cartesia_voice_id": LaunchConfiguration("cartesia_voice_id"),
                    }
                ],
            ),
        ]
    )

