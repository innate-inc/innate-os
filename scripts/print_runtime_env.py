#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None

ENV_KEYS_MOVED_TO_OS_CONFIG = {
    "BRAIN_WEBSOCKET_URI",
    "TELEMETRY_URL",
    "CARTESIA_VOICE_ID",
}


def parse_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        env[key.strip()] = value
    return env


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


def _parse_toml_scalar(raw_value: str) -> object:
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


def parse_toml_file(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    if tomllib is not None:
        with path.open("rb") as f:
            data = tomllib.load(f)
        return data if isinstance(data, dict) else {}

    data: dict[str, object] = {}
    current_section: dict[str, object] = data
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
                current_section = current_section.setdefault(key, {})  # type: ignore[assignment]
            continue
        if "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        current_section[key] = _parse_toml_scalar(raw_value)
    return data


def parse_os_config(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data = parse_toml_file(path)

    env: dict[str, str] = {}
    brain = data.get("brain", {}) if isinstance(data, dict) else {}
    telemetry = data.get("telemetry", {}) if isinstance(data, dict) else {}
    voice = data.get("voice", {}) if isinstance(data, dict) else {}

    websocket_uri = brain.get("websocket_uri") if isinstance(brain, dict) else None
    telemetry_url = telemetry.get("url") if isinstance(telemetry, dict) else None
    cartesia_voice_id = voice.get("cartesia_voice_id") if isinstance(voice, dict) else None

    if isinstance(websocket_uri, str) and websocket_uri.strip():
        env["BRAIN_WEBSOCKET_URI"] = websocket_uri.strip()
    if isinstance(telemetry_url, str) and telemetry_url.strip():
        env["TELEMETRY_URL"] = telemetry_url.strip()
    if isinstance(cartesia_voice_id, str) and cartesia_voice_id.strip():
        env["CARTESIA_VOICE_ID"] = cartesia_voice_id.strip()
    return env


def build_runtime_env(repo_root: Path) -> dict[str, str]:
    env = parse_os_config(repo_root / "config" / "os.toml")
    for key, value in parse_env_file(repo_root / ".env").items():
        if key in ENV_KEYS_MOVED_TO_OS_CONFIG:
            continue
        env[key] = value
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description="Print merged Innate OS runtime environment.")
    parser.add_argument("--shell", action="store_true", help="Print shell export commands")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    env = build_runtime_env(repo_root)

    if args.shell:
        print("; ".join(f"export {key}={shlex.quote(value)}" for key, value in sorted(env.items())))
        return 0

    for key, value in sorted(env.items()):
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
