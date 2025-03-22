#!/usr/bin/env python3
"""
Logging configuration for the brain_client package.
This module defines the environment variables for configuring ROS2 logging.
"""

from launch.actions import SetEnvironmentVariable


def get_logging_env_vars():
    """
    Returns a list of SetEnvironmentVariable actions for configuring ROS2 logging.

    Returns:
        list: A list of SetEnvironmentVariable actions.
    """
    return [
        # Format: [level] message
        # This removes timestamps and node names for more concise logs
        SetEnvironmentVariable(
            "RCUTILS_CONSOLE_OUTPUT_FORMAT", "[{severity}] {message}"
        ),
        # Enable colorized output
        SetEnvironmentVariable("RCUTILS_COLORIZED_OUTPUT", "1"),
    ]
