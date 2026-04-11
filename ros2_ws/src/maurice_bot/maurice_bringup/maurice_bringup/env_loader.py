#!/usr/bin/env python3
"""
Environment loader for Innate-OS.
Loads .env file and provides access to environment variables.
"""

import os
from pathlib import Path
from typing import Optional


def load_env_file(env_path: Optional[Path] = None) -> None:
    """
    Load environment variables from .env file.
    
    Args:
        env_path: Optional path to .env file. If not provided, uses INNATE_OS_ROOT
                  or defaults to ~/innate-os/.env
    """
    if env_path is None:
        innate_root = os.environ.get(
            'INNATE_OS_ROOT', 
            os.path.join(os.path.expanduser('~'), 'innate-os')
        )
        env_path = Path(innate_root) / ".env"
    
    if not env_path.exists():
        return
    
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                value = value.strip()
                # Handle quoted values (single or double quotes)
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                os.environ[key.strip()] = value


def get_env(key: str, default: str = "") -> str:
    """
    Get environment variable, loading .env if not already loaded.
    
    Args:
        key: Environment variable name
        default: Default value if not found
        
    Returns:
        Environment variable value or default
    """
    return os.environ.get(key, default)


# Load env file on module import
if __name__ == "__main__":
    load_env_file()
