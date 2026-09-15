# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Vendor API keys for the Settings page: written to the robot's ``.env``, never read back.

Keys stay out of settings.yaml on purpose — that file is a ROS parameter overlay,
and a parameter is readable by every node and by anything on the graph, this
page over rosbridge included. ``.env`` is what the nodes load at boot
(mars_bringup.config_loader), so a key saved here reaches them on the next
restart. The page learns only whether a key is set and its last characters.
"""

import os
import re
import tempfile
import threading
from pathlib import Path

KEYS = ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_API_KEY")
SERVICE_KEY = "INNATE_SERVICE_KEY"
SYSTEM_ENV_PATH = Path("/etc/innate.env")  # the provisioned service key lives here, read-only to this page

_WRITE_LOCK = threading.Lock()
_HINT_CHARS = 4
_MAX_LEN = 512


def env_path() -> Path:
    root = os.environ.get("INNATE_OS_ROOT", os.path.expanduser("~/innate-os"))
    return Path(root) / ".env"


def read_status() -> dict:
    """``{keys: {NAME: {set, hint}}, service_key}`` — never a value."""
    values = {**_values(SYSTEM_ENV_PATH), **_values(env_path())}
    keys = {name: {"set": bool(values.get(name)), "hint": _hint(values.get(name, ""))} for name in KEYS}
    return {"keys": keys, "service_key": bool(values.get(SERVICE_KEY))}


def apply(sets: dict, clears: list) -> tuple[bool, str]:
    """Set and clear keys in ``.env``, touching only their lines. Returns ``(ok, message)``."""
    for name in [*sets, *clears]:
        if name not in KEYS:
            return False, f"{name} is not a key this page manages"
    for name, value in sets.items():
        if not isinstance(value, str) or not value.strip():
            return False, f"{name}: a key must not be empty"
        if len(value) > _MAX_LEN or any(c in value for c in "\r\n\"'"):
            return False, f"{name}: that does not look like an API key"
    with _WRITE_LOCK:
        return _apply_locked({name: value.strip() for name, value in sets.items()}, list(clears))


def _apply_locked(sets: dict, clears: list) -> tuple[bool, str]:
    path = env_path()
    try:
        existed = path.is_file()
        lines = path.read_text().splitlines() if existed else []
    except OSError as e:
        return False, f"could not read {path}: {e}"
    for name, value in sets.items():
        lines = _with_line(lines, name, f"{name}={_render(value)}")
    for name in clears:
        lines = _with_line(lines, name, f"# {name}=")
    text = "\n".join(lines) + ("\n" if lines else "")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        # A key file is the operator's alone; a pre-existing .env keeps whatever mode it had.
        os.chmod(tmp, path.stat().st_mode & 0o777 if existed else 0o600)
        os.replace(tmp, str(path))
    except OSError as e:
        return False, f"could not write {path}: {e}"
    return True, "saved — takes effect on the next restart"


def _with_line(lines: list, name: str, new: str) -> list:
    """``lines`` with ``name``'s line replaced by ``new``: the active line if there is one (later
    duplicates dropped, they would shadow it), else the template's ``# NAME=`` placeholder in
    place, else appended — so the file keeps its shape and its comments."""
    active = re.compile(rf"^\s*{name}\s*=")
    placeholder = re.compile(rf"^\s*#\s*{name}\s*=\s*$")
    hits = [i for i, line in enumerate(lines) if active.match(line)]
    if hits:
        first, *rest = hits
        lines[first] = new
        return [line for i, line in enumerate(lines) if i not in rest]
    for i, line in enumerate(lines):
        if placeholder.match(line):
            lines[i] = new
            return lines
    if not new.startswith("#"):
        lines.append(new)
    return lines


def _render(value: str) -> str:
    """Quoted only when the loader would otherwise misread it (a space or a ``#``)."""
    return value if re.fullmatch(r"[^\s#]+", value) else f'"{value}"'


def _values(path: Path) -> dict:
    """The same reading the nodes give ``.env`` (mars_bringup.config_loader._load_key_value_env)."""
    try:
        text = path.read_text()
    except OSError:
        return {}
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _hint(value: str) -> str:
    if not value:
        return ""
    return "…" + value[-_HINT_CHARS:] if len(value) > 2 * _HINT_CHARS else "set"
