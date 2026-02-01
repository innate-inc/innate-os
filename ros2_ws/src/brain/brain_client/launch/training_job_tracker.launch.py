import os
from pathlib import Path

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

from brain_client.logging_config import get_logging_env_vars


def generate_launch_description():
    # Load environment variables from .env file if it exists
    innate_os_root = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))
    env_file_path = Path(innate_os_root) / ".env"
    if env_file_path.exists():
        with open(env_file_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ[key.strip()] = value.strip()

    # Get logging environment variables
    env_vars = get_logging_env_vars()

    # Declare launch arguments
    download_dir_arg = DeclareLaunchArgument(
        "download_dir",
        default_value="/home/jetson1/innate-os/skills",
        description="Directory to download trained models to",
    )
    poll_interval_running_arg = DeclareLaunchArgument(
        "poll_interval_running",
        default_value="300",
        description="Poll interval in seconds for running jobs (default: 5 minutes)",
    )
    poll_interval_submitted_arg = DeclareLaunchArgument(
        "poll_interval_submitted",
        default_value="120",
        description="Poll interval in seconds for submitted jobs (default: 2 minutes)",
    )
    poll_interval_uploading_arg = DeclareLaunchArgument(
        "poll_interval_uploading",
        default_value="60",
        description="Poll interval in seconds for uploading jobs (default: 1 minute)",
    )

    return LaunchDescription(
        env_vars
        + [
            download_dir_arg,
            poll_interval_running_arg,
            poll_interval_submitted_arg,
            poll_interval_uploading_arg,
            Node(
                package="brain_client",
                executable="training_job_tracker_node.py",
                name="training_job_tracker_node",
                output="screen",
                parameters=[
                    {
                        "download_dir": LaunchConfiguration("download_dir"),
                        "poll_interval_running": LaunchConfiguration("poll_interval_running"),
                        "poll_interval_submitted": LaunchConfiguration("poll_interval_submitted"),
                        "poll_interval_uploading": LaunchConfiguration("poll_interval_uploading"),
                    }
                ],
            ),
        ]
    )
