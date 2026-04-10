from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from brain_client.logging_config import get_logging_env_vars
from maurice_bringup.env_loader import load_env_file


def generate_launch_description():
    # Load environment variables from .env file if it exists.
    # Also pulls /etc/innate/.env as a last-resort fallback.
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
        default_value="9fdaae0b-f885-4813-b589-3c07cf9d5fea",
        description="Cartesia Alfred voice id",
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

