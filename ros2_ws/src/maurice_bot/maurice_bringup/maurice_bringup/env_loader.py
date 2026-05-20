#!/usr/bin/env python3
"""
Environment loader for Innate-OS.
Loads .env file and provides access to environment variables.
"""

import os
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

ENV_KEYS_MOVED_TO_OS_CONFIG = {
    "BRAIN_WEBSOCKET_URI",
    "TELEMETRY_URL",
    "CARTESIA_VOICE_ID",
}


def _load_key_value_env(path: Path) -> None:
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                if key in ENV_KEYS_MOVED_TO_OS_CONFIG:
                    continue
                value = value.strip()
                if (value.startswith('"') and value.endswith('"')) or \
                   (value.startswith("'") and value.endswith("'")):
                    value = value[1:-1]
                os.environ[key] = value


def _strip_toml_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    chars: list[str] = []

    for char in value:
        if escaped:
            chars.append(char)
            escaped = False
            continue
        if char == "\\" and (in_single or in_double):
            chars.append(char)
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            chars.append(char)
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            chars.append(char)
            continue
        if char == "#" and not in_single and not in_double:
            break
        chars.append(char)

    return "".join(chars).strip()


def _parse_toml_scalar(raw_value: str):
    value = _strip_toml_comment(raw_value).strip()
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {"'", '"'}
    ):
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def _parse_toml_file(path: Path) -> dict:
    if not path.exists():
        return {}
    if tomllib is not None:
        with path.open("rb") as f:
            data = tomllib.load(f)
        return data if isinstance(data, dict) else {}

    data: dict = {}
    current_section: dict = data
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section_name = line[1:-1].strip()
            if not section_name:
                continue
            current_section = data
            for key in section_name.split("."):
                current_section = current_section.setdefault(key, {})
            continue
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        current_section[key] = _parse_toml_scalar(raw_value)
    return data


def _load_os_config(path: Path) -> None:
    if not path.exists():
        return

    data = _parse_toml_file(path)

    brain = data.get("brain", {}) if isinstance(data, dict) else {}
    telemetry = data.get("telemetry", {}) if isinstance(data, dict) else {}
    voice = data.get("voice", {}) if isinstance(data, dict) else {}

    websocket_uri = brain.get("websocket_uri") if isinstance(brain, dict) else None
    telemetry_url = telemetry.get("url") if isinstance(telemetry, dict) else None
    cartesia_voice_id = voice.get("cartesia_voice_id") if isinstance(voice, dict) else None

    if isinstance(websocket_uri, str) and websocket_uri.strip():
        os.environ.setdefault("BRAIN_WEBSOCKET_URI", websocket_uri.strip())
    if isinstance(telemetry_url, str) and telemetry_url.strip():
        os.environ.setdefault("TELEMETRY_URL", telemetry_url.strip())
    if isinstance(cartesia_voice_id, str) and cartesia_voice_id.strip():
        os.environ.setdefault("CARTESIA_VOICE_ID", cartesia_voice_id.strip())


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

    innate_root = env_path.parent
    _load_os_config(innate_root / "config" / "os.toml")
    _load_key_value_env(env_path)


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
