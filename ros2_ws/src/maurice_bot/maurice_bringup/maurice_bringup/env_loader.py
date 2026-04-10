#!/usr/bin/env python3
"""
Environment loader for Innate-OS.
Loads .env file and provides access to environment variables.
"""

import os
from pathlib import Path
from typing import Optional

# System-wide fallback env file (populated by post_update.sh)
SYSTEM_ENV_PATH = Path("/etc/innate/.env")


def _parse_env_file(path: Path, override: bool = True) -> None:
    """Parse a .env file into os.environ. Silently no-ops if missing/unreadable."""
    try:
        if not path.exists():
            return
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    # Handle quoted values (single or double quotes)
                    if (value.startswith('"') and value.endswith('"')) or \
                       (value.startswith("'") and value.endswith("'")):
                        value = value[1:-1]
                    if override or key not in os.environ:
                        os.environ[key] = value
    except OSError:
        # Permission denied, file vanished, etc. — fail gracefully.
        return


def load_env_file(env_path: Optional[Path] = None) -> None:
    """
    Load environment variables from .env file.

    Args:
        env_path: Optional path to .env file. If not provided, uses INNATE_OS_ROOT
                  or defaults to ~/innate-os/.env

    Also reads ``/etc/innate/.env`` as a last-resort fallback for any keys
    not already set by the primary file or the existing environment.
    """
    if env_path is None:
        innate_root = os.environ.get(
            'INNATE_OS_ROOT',
            os.path.join(os.path.expanduser('~'), 'innate-os')
        )
        env_path = Path(innate_root) / ".env"

    _parse_env_file(env_path, override=True)
    # Last resort: system-wide env file written by post_update.sh
    _parse_env_file(SYSTEM_ENV_PATH, override=False)


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
