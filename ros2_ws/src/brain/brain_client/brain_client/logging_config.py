#!/usr/bin/env python3
"""
Logging configuration for the brain_client package.
This module defines the environment variables for configuring ROS2 logging
and provides a universal Logger class that works with ROS loggers and regular loggers.
"""

import os
from launch.actions import SetEnvironmentVariable


class UniversalLogger:
    """
    Universal logger wrapper that handles enabled/disabled state internally.
    
    Works with ROS loggers (from rclpy.node.Node.get_logger()) and regular loggers.
    Can be used unconditionally - the logger handles whether to actually log.
    
    Supports optional boolean parameters for conditional logging per method call.
    """
    
    def __init__(self, enabled: bool = True, wrapped_logger=None):
        """
        Initialize logger.
        
        Args:
            enabled: Whether logging is enabled (default: True)
            wrapped_logger: Optional existing logger to wrap (ROS logger or any logger with info/debug/error/warning methods)
        """
        # Check DEBUG env var if enabled not explicitly set
        self._enabled = enabled
        if enabled:
            debug_env = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
            # If DEBUG is set, it overrides enabled=False
            if debug_env:
                self._enabled = True
        
        self._wrapped = wrapped_logger
    
    def _should_log(self, condition: bool = True) -> bool:
        """Check if logging should occur."""
        return self._enabled and condition
    
    def info(self, msg: str, condition: bool = True):
        """Log info message."""
        if self._should_log(condition):
            if self._wrapped:
                self._wrapped.info(msg)
            else:
                print(f"[INFO] {msg}")
    
    def debug(self, msg: str, condition: bool = True):
        """Log debug message."""
        if self._should_log(condition):
            if self._wrapped:
                self._wrapped.debug(msg)
            else:
                print(f"[DEBUG] {msg}")
    
    def error(self, msg: str, condition: bool = True):
        """Log error message."""
        if self._should_log(condition):
            if self._wrapped:
                self._wrapped.error(msg)
            else:
                print(f"[ERROR] {msg}")
    
    def warning(self, msg: str, condition: bool = True):
        """Log warning message."""
        if self._should_log(condition):
            if self._wrapped:
                self._wrapped.warning(msg)
            else:
                print(f"[WARNING] {msg}")
    
    def warn(self, msg: str, condition: bool = True):
        """Alias for warning."""
        self.warning(msg, condition=condition)


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
