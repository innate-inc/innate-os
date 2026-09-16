# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Vendor API keys for the Settings page: written to the robot's ``.env``, never read back.

One managed name is not a credential — an Anthropic workspace id, which an
organization-scoped key must name — and is reported in full; every key reports only
whether it is set and its last characters.

Keys stay out of settings.yaml on purpose — that file is a ROS parameter overlay,
and a parameter is readable by every node and by anything on the graph, this
page over rosbridge included. ``.env`` is what the nodes load at boot
(mars_bringup.config_loader), so a key saved here reaches them on the next
restart. The page learns only whether a key is set and its last characters.
"""

import contextlib
import errno
import os
import re
import tempfile
import threading
from pathlib import Path

KEYS = ("GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_WORKSPACE_ID", "LLM_API_KEY")
SECRETS = frozenset(KEYS) - {"ANTHROPIC_WORKSPACE_ID"}
SERVICE_KEY = "INNATE_SERVICE_KEY"
SYSTEM_ENV_PATH = Path("/etc/innate.env")  # the provisioned service key lives here, read-only to this page

_WRITE_LOCK = threading.Lock()
_HINT_CHARS = 4
_MAX_LEN = 512


KEYS_ENV_FILE = "INNATE_KEYS_ENV_FILE"


def env_path() -> Path:
    """The .env keys are written to — the robot's own, or the file a deployment points here.

    The sim runs the nodes against a file its launcher generates from the checkout's .env on
    every ``up``; writing the generated copy would lose every key at the next start, so the
    launcher points this at the source instead."""
    override = os.environ.get(KEYS_ENV_FILE, "").strip()
    if override:
        return Path(override)
    root = os.environ.get("INNATE_OS_ROOT", os.path.expanduser("~/innate-os"))
    return Path(root) / ".env"


def read_status() -> dict:
    """``{keys: {NAME: {set, hint}}, service_key}`` — never a value."""
    values = {**_values(SYSTEM_ENV_PATH), **_values(env_path())}
    keys = {}
    for name in KEYS:
        value = values.get(name, "")
        keys[name] = {"set": bool(value), "hint": _hint(value) if name in SECRETS else value}
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
            return False, f"{name}: that does not look like a key or an id"
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
        _write(path, text, existed=existed)
    except OSError as e:
        return False, f"could not write {path}: {e}"
    return True, "saved — takes effect on the next restart"


# A rename onto a bind-mounted file fails with these however writable the file is; every
# other OSError (no space, no permission, I/O) must not reach the write-through below,
# which truncates the operator's keys before it can fail the same way.
_MOUNTED_OVER = frozenset({errno.EBUSY, errno.EXDEV, errno.EINVAL})


def _write(path: Path, text: str, *, existed: bool) -> None:
    """Replace the file atomically, else — only for a bind mount — write through it.

    The sim bind-mounts the host's ``.env`` onto this path, so the rename cannot work there
    and the alternative is a Settings page that cannot save a key in the sim at all. The
    file's previous bytes go back if that write fails partway."""
    # A key file is the operator's alone; a pre-existing .env keeps whatever mode it had.
    mode = path.stat().st_mode & 0o777 if existed else 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except OSError as error:
        if error.errno not in _MOUNTED_OVER:
            raise
        previous = path.read_bytes() if existed else b""
        try:
            with open(path, "w") as f:
                f.write(text)
        except OSError:
            with contextlib.suppress(OSError):
                path.write_bytes(previous)
            raise
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


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
