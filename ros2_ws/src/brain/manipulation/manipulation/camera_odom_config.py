#!/usr/bin/env python3
"""
Shared configuration and utilities for camera_odom_recorder and camera_odom_server.

This module contains common constants, paths, and helper functions used by both
the recorder (which captures data) and the server (which provides WebSocket access).
"""

import os
import json
from typing import List, Dict, Optional

# Default paths
DEFAULT_RECORDING_DIR = '~/innate-os/camera_odom_recordings'
DEFAULT_SESSION_PREFIX = 'session'

# File names within each session directory
H5_FILENAME = 'recording.h5'
METADATA_FILENAME = 'metadata.json'

# Default recording settings
DEFAULT_DATA_FREQUENCY = 10  # Hz
DEFAULT_CHUNK_SIZE = 100
DEFAULT_WEBSOCKET_PORT = 8771

# ROS service names (relative to brain/ namespace)
SERVICE_START_RECORDING = 'brain/camera_odom_recorder/start_recording'
SERVICE_STOP_RECORDING = 'brain/camera_odom_recorder/stop_recording'
SERVICE_GET_STATUS = 'brain/camera_odom_recorder/get_status'


def get_recording_dir(custom_path: Optional[str] = None) -> str:
    """
    Get the recording directory path, expanded.
    
    Args:
        custom_path: Optional custom path. If None, uses DEFAULT_RECORDING_DIR.
    
    Returns:
        Expanded absolute path to the recording directory.
    """
    path = custom_path if custom_path else DEFAULT_RECORDING_DIR
    return os.path.expanduser(path)


def get_session_path(recording_dir: str, session_name: str) -> str:
    """Get the path to a specific session directory."""
    return os.path.join(recording_dir, session_name)


def get_h5_path(session_dir: str) -> str:
    """Get the path to the HDF5 file within a session directory."""
    return os.path.join(session_dir, H5_FILENAME)


def get_metadata_path(session_dir: str) -> str:
    """Get the path to the metadata file within a session directory."""
    return os.path.join(session_dir, METADATA_FILENAME)


def list_available_sessions(recording_dir: str) -> List[Dict]:
    """
    List all available recording sessions in the given directory.
    
    Args:
        recording_dir: Path to the recordings directory.
    
    Returns:
        List of session info dicts with keys:
        - name: Session name (directory name)
        - has_metadata: Whether metadata.json exists
        - size_mb: Size of recording.h5 in MB
        - num_frames: Number of frames (from metadata, if available)
        - duration_sec: Duration in seconds (from metadata, if available)
    """
    sessions = []
    
    if not os.path.exists(recording_dir):
        return sessions
    
    for name in sorted(os.listdir(recording_dir)):
        session_path = os.path.join(recording_dir, name)
        
        if not os.path.isdir(session_path):
            continue
        
        h5_path = get_h5_path(session_path)
        metadata_path = get_metadata_path(session_path)
        
        if not os.path.exists(h5_path):
            continue
        
        session_info = {
            'name': name,
            'has_metadata': os.path.exists(metadata_path),
            'size_mb': os.path.getsize(h5_path) / (1024 * 1024)
        }
        
        # Load additional info from metadata if available
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path, 'r') as f:
                    meta = json.load(f)
                session_info['num_frames'] = meta.get('num_frames', 0)
                session_info['duration_sec'] = meta.get('duration_sec', 0)
            except (json.JSONDecodeError, IOError):
                pass
        
        sessions.append(session_info)
    
    return sessions


def load_session_metadata(session_dir: str) -> Dict:
    """
    Load metadata for a session.
    
    Args:
        session_dir: Path to the session directory.
    
    Returns:
        Metadata dict, or empty dict if not found.
    """
    metadata_path = get_metadata_path(session_dir)
    
    if not os.path.exists(metadata_path):
        return {}
    
    try:
        with open(metadata_path, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}

