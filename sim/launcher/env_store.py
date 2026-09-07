# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Private, atomic updates to the local dotenv file shared by the CLI and wizard."""

from __future__ import annotations

import fcntl
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def _validate_env_key(key: str) -> None:
    if not isinstance(key, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
        raise ValueError("Invalid environment variable name")


def _quote_env_value(value: str, *, allow_empty: bool = False) -> str:
    # These files are also sourced by shell launchers. Never accept quoting or
    # expansion syntax, even though our own assignments use literal quotes.
    if not isinstance(value, str) or (not value.strip() and not (allow_empty and value == "")):
        raise ValueError("Environment value must not be empty")
    if any(not ch.isprintable() or ch in "\"'$`\\" for ch in value):
        raise ValueError("Environment value contains unsupported characters")
    return f"'{value}'"


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _is_active_env_assignment(line: str, key: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return False
    assignment_key, _ = stripped.split("=", 1)
    return assignment_key.strip() == key


def _is_commented_env_assignment(line: str, key: str) -> bool:
    """Match commented assignments, never descriptive comments mentioning a key."""
    stripped = line.strip()
    return stripped.startswith("#") and _is_active_env_assignment(stripped.lstrip("#").strip(), key)


def _check_regular_or_missing(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Environment storage must be a regular file, not a symlink")


@contextmanager
def _locked_env(path: Path) -> Iterator[list[str] | None]:
    """Hold a stable sidecar lock through the complete read/modify/replace."""
    _check_regular_or_missing(path)
    lock_path = path.with_name(path.name + ".lock")
    _check_regular_or_missing(lock_path)
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    lock_fd = os.open(lock_path, flags, 0o600)
    try:
        lock_info = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_info.st_mode):
            raise ValueError("Environment lock must be a regular file")
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        # Do not continue on a lock inode that was replaced while we waited.
        current_lock = lock_path.lstat()
        if (current_lock.st_dev, current_lock.st_ino) != (lock_info.st_dev, lock_info.st_ino):
            raise ValueError("Environment lock changed during update")
        _check_regular_or_missing(path)
        try:
            env_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            lines = None
        else:
            with os.fdopen(env_fd, "r", encoding="utf-8") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ValueError("Environment storage must be a regular file")
                # Tighten permissions on old files as well as replacements.
                os.fchmod(source.fileno(), 0o600)
                try:
                    lines = source.read().splitlines()
                except UnicodeError:
                    raise ValueError("Environment storage must contain valid UTF-8 text") from None
        yield lines
    finally:
        os.close(lock_fd)


def _atomic_write(path: Path, lines: list[str]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as destination:
            os.fchmod(destination.fileno(), 0o600)
            destination.write("\n".join(lines) + "\n")
            destination.flush()
            os.fsync(destination.fileno())
        _check_regular_or_missing(path)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_env_value(path: Path, key: str, value: str, *, allow_empty: bool = False) -> None:
    """Save one value, removing duplicate and stale commented assignments.

    Empty values are rejected unless ``allow_empty=True`` explicitly requests a
    blank tombstone. Errors never include the supplied key or value.
    """
    _validate_env_key(key)
    replacement = f"{key}={_quote_env_value(value, allow_empty=allow_empty)}"
    with _locked_env(path) as lines:
        updated = False
        output: list[str] = []
        for line in lines or []:
            if _is_active_env_assignment(line, key):
                if not updated:
                    output.append(replacement)
                    updated = True
            elif not _is_commented_env_assignment(line, key):
                output.append(line)
        if not updated:
            if output and output[-1].strip():
                output.append("")
            output.append(replacement)
        _atomic_write(path, output)


def write_env_values(path: Path, values: dict[str, str]) -> None:
    """Replace a generated Docker env file; values are literal, never shell sourced.

    Docker's env-file syntax does not unquote values. Keep them unquoted, and
    retain explicit empty values to mask credentials inherited from an image.
    """
    output = []
    for key, value in sorted(values.items()):
        _validate_env_key(key)
        if not isinstance(value, str) or any(not ch.isprintable() for ch in value):
            raise ValueError("Environment value contains unsupported characters")
        output.append(f"{key}={value}")
    with _locked_env(path):
        _atomic_write(path, output)


def comment_out_env_key(path: Path, key: str) -> bool:
    """Disable an active assignment, retaining the effective value for switching.

    Return whether an active assignment was found. Older commented copies are
    removed so restoring a backend cannot resurrect an obsolete credential.
    """
    _validate_env_key(key)
    with _locked_env(path) as lines:
        lines = lines or []
        active = [i for i, line in enumerate(lines) if _is_active_env_assignment(line, key)]
        if not active:
            return False
        output = []
        for i, line in enumerate(lines):
            if i == active[-1]:
                output.append(f"# {line}")
            elif not _is_active_env_assignment(line, key) and not _is_commented_env_assignment(line, key):
                output.append(line)
        _atomic_write(path, output)
        return True


def uncomment_env_key(path: Path, key: str) -> str | None:
    """Restore a saved backend key, skipping blank template placeholders.

    An active assignment (including a blank tombstone) takes precedence. The
    restored value is validated and quoted before becoming shell-readable.
    """
    _validate_env_key(key)
    with _locked_env(path) as lines:
        lines = lines or []
        if any(_is_active_env_assignment(line, key) for line in lines):
            return None
        for i, line in enumerate(lines):
            if not _is_commented_env_assignment(line, key):
                continue
            body = line.strip().lstrip("#").strip()
            candidate = _unquote_env_value(body.split("=", 1)[1].strip())
            if not candidate.strip():
                continue
            replacement = f"{key}={_quote_env_value(candidate)}"
            output = [
                replacement if j == i else current
                for j, current in enumerate(lines)
                if j == i or not _is_commented_env_assignment(current, key)
            ]
            _atomic_write(path, output)
            return candidate
        return None
