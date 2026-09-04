# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

from config import (
    CARTESIA_API_KEY,
    CLI_SIM,
    ENV_PATH,
    GEMINI_API_KEY,
    INNATE_SERVICE_KEY,
    SECRET_ENV_KEYS,
    is_configured_secret_value,
    success,
    warn,
)
from dashboard import BOLD, CYAN, DIM, GREEN, NC, YELLOW, confirm, menus_supported, select_one
from runtime import UV_INSTALL_COMMAND, find_uv


def _split_option(label: str) -> tuple[str, str]:
    """Split `Name (hint)` into its parts, so the menu can dim the hint. The
    typed prompt shows the same strings whole."""
    name, sep, hint = label.partition(" (")
    return (name, hint.rstrip(")")) if sep else (label, "")


def is_interactive_terminal() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _is_active_env_assignment(line: str, key: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return False
    assignment_key, _ = stripped.split("=", 1)
    return assignment_key.strip() == key


def _prompt_yes_no(question: str, *, default: bool = False) -> bool:
    if menus_supported():
        try:
            return confirm(question, default=default)
        except (KeyboardInterrupt, EOFError):
            print()
            raise SystemExit(1)  # noqa: B904
    default_label = "Y/n" if default else "y/N"
    while True:
        try:
            value = input(f"{YELLOW}{question} [{default_label}]: {NC}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            raise SystemExit(1)  # noqa: B904
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print(f"{YELLOW}Please enter y or n.{NC}")


def _prompt_secret(question: str) -> str:
    prompt = f"{YELLOW}{question}: {NC}"
    try:
        masked = _read_masked_secret(prompt)
        # None: no interactive TTY (or raw mode unavailable) -- fall back to
        # fully hidden input rather than echoing to a non-terminal.
        if masked is None:
            return getpass.getpass(prompt, stream=sys.stdout).strip()
        return masked
    except (KeyboardInterrupt, EOFError):
        print()
        raise SystemExit(1)  # noqa: B904


def _read_masked_secret(prompt: str) -> str | None:
    """Read a line echoing '*' per character so a paste is visibly registered,
    with a live length count so a double-paste is obvious (Ctrl-U clears).
    Returns None when stdin/stdout isn't an interactive TTY or raw mode is
    unavailable, letting the caller fall back to hidden input."""
    try:
        import termios
        import tty
    except ImportError:
        return None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    chars: list[str] = []

    def redraw() -> None:
        count = f" ({len(chars)})" if chars else ""
        sys.stdout.write("\r\x1b[K" + prompt + "*" * len(chars) + count)
        sys.stdout.flush()

    try:
        # Raw so Ctrl-C/Ctrl-U/backspace arrive as bytes we handle here.
        tty.setraw(fd)
        redraw()
        while (ch := sys.stdin.read(1)) not in ("\r", "\n"):
            if ch == "":  # stdin closed
                raise EOFError
            if ch == "\x03":  # Ctrl-C (raw mode swallows the signal)
                raise KeyboardInterrupt
            if ch == "\x15":  # Ctrl-U: clear a botched/double paste and retry
                chars.clear()
            elif ch in ("\x7f", "\b"):  # backspace
                if chars:
                    chars.pop()
            elif ch > " ":  # printable non-space (keys never contain spaces)
                chars.append(ch)
            redraw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\n")
        sys.stdout.flush()
    return "".join(chars).strip()


def _quote_env_value(value: str) -> str:
    if "'" in value:
        raise ValueError("secret values saved to .env cannot contain single quotes")
    return f"'{value}'"


def _unquote_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _is_commented_env_assignment(line: str, key: str) -> bool:
    """True only for a commented-out assignment of ``key`` (``# KEY=value``).

    A descriptive comment like ``# Filled by ./innate setup ...`` is not an
    assignment, so it is never matched (and never toggled)."""
    stripped = line.strip()
    if not stripped.startswith("#"):
        return False
    return _is_active_env_assignment(stripped.lstrip("#").strip(), key)


def write_env_value(path: Path, key: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{key} cannot contain newlines")

    replacement = f"{key}={_quote_env_value(value)}"
    lines = path.read_text().splitlines() if path.exists() else []
    updated = False
    output: list[str] = []

    for line in lines:
        if _is_active_env_assignment(line, key):
            if not updated:
                output.append(replacement)
                updated = True
        else:
            output.append(line)

    if not updated:
        if output and output[-1].strip():
            output.append("")
        output.append(replacement)

    path.write_text("\n".join(output) + "\n")


def comment_out_env_key(path: Path, key: str) -> bool:
    """Comment out an active ``KEY=...`` assignment in the env file, if present.

    Returns True when a line was actually commented out. Leaves unset keys and
    already-commented lines untouched.
    """
    if not path.exists():
        return False
    lines = path.read_text().splitlines()
    changed = False
    output: list[str] = []
    for line in lines:
        if _is_active_env_assignment(line, key):
            output.append(f"# {line}")
            changed = True
        else:
            output.append(line)
    if changed:
        path.write_text("\n".join(output) + "\n")
    return changed


def uncomment_env_key(path: Path, key: str) -> str | None:
    """Re-enable a previously commented-out ``# KEY=value`` assignment so the user
    can switch a backend back on without re-pasting the key. Value-less
    placeholders (the ``# KEY=`` lines shipped in .env.template) are left
    commented — there is no key to restore, so the caller must prompt for one.
    Returns the restored value, or None if there is nothing to restore."""
    if not path.exists():
        return None
    lines = path.read_text().splitlines()
    value: str | None = None
    output: list[str] = []
    for line in lines:
        if value is None and _is_commented_env_assignment(line, key):
            body = line.strip().lstrip("#").strip()
            _, raw_value = body.split("=", 1)
            candidate = _unquote_env_value(raw_value.strip())
            if candidate:
                output.append(body)
                value = candidate
                continue
        output.append(line)
    if value is not None:
        path.write_text("\n".join(output) + "\n")
    return value


def _use_key_for_run(config: dict[str, object], key: str, value: str) -> None:
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    raw_env[key] = value
    user_env[key] = value


def _save_key(config: dict[str, object], key: str, value: str) -> None:
    write_env_value(ENV_PATH, key, value)
    _use_key_for_run(config, key, value)
    success(f"Saved {key} to {ENV_PATH}.")


def _prompt_choice(question: str, options: dict[str, str], *, default: str) -> str:
    if menus_supported():
        keys = list(options)
        try:
            chosen = select_one(
                question,
                [_split_option(options[key]) for key in keys],
                default=keys.index(default),
            )
        except (KeyboardInterrupt, EOFError):
            print()
            raise SystemExit(1)  # noqa: B904
        return keys[chosen]

    print(f"{YELLOW}{question}{NC}")
    for key, label in options.items():
        marker = "  (default)" if key == default else ""
        print(f"  {BOLD}{key}{NC}) {label}{DIM}{marker}{NC}")
    while True:
        try:
            value = input(f"{YELLOW}Choose [{default}]: {NC}").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            raise SystemExit(1)  # noqa: B904
        if not value:
            return default
        if value in options:
            return value
        print(f"{YELLOW}Please choose one of: {', '.join(options)}.{NC}")


def _configure_key(config: dict[str, object], key: str, prompt: str, *, optional: bool = False) -> bool:
    """Get one secret into .env -- keep what is already there, restore a
    commented-out line, adopt one from the shell, or ask for it. Returns
    whether the key ends up set; an optional one is declined with an empty
    line, a required one asks again.
    """
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    if is_configured_secret_value(key, user_env.get(key)):
        if not _prompt_yes_no(f"{key} is already set. Replace it?", default=False):
            return True
    else:
        restored = uncomment_env_key(ENV_PATH, key)
        if restored is not None:
            _use_key_for_run(config, key, restored)
            success(f"Re-enabled {key} in {ENV_PATH.name}.")
            return True

        shell_value = os.environ.get(key, "").strip()
        if is_configured_secret_value(key, shell_value) and _prompt_yes_no(
            f"Found {key} in your shell. Save it to {ENV_PATH.name}?", default=True
        ):
            _save_key(config, key, shell_value)
            return True

    while True:
        value = _prompt_secret(prompt)
        if is_configured_secret_value(key, value):
            _save_key(config, key, value)
            return True
        if optional:
            return False
        warn(f"{key} cannot be empty. Press Ctrl+C to cancel.")


def _configure_own_keys(config: dict[str, object]) -> None:
    """Your own keys: Gemini for the brain, then Cartesia for the voice that a
    service key would otherwise include."""
    _configure_key(config, GEMINI_API_KEY, f"Paste {GEMINI_API_KEY}")
    print(f"{DIM}A Cartesia key gives the robot a voice. Everything else works without it.{NC}")
    if not _configure_key(config, CARTESIA_API_KEY, f"Paste {CARTESIA_API_KEY} (Enter to skip)", optional=True):
        warn("No Cartesia key — the robot will run without speech.")


def ensure_uv_prerequisite() -> None:
    """uv runs the sim world (MuJoCo physics + rendering) on the host --
    `up` requires it. Offer the official installer interactively;
    non-interactive runs just report the command."""
    if find_uv() is not None:
        success("uv is installed.")
        return
    if not is_interactive_terminal():
        warn(f"uv is not installed (required by `{CLI_SIM} up`). Install it with: {UV_INSTALL_COMMAND}")
        return
    print(f"{DIM}uv runs the sim world (physics + rendering) on the host; `{CLI_SIM} up` requires it.{NC}")
    if not _prompt_yes_no(
        "uv is not installed. Install it now (official installer, user-local, no sudo)?", default=True
    ):
        warn(f"Skipped. Install it before `{CLI_SIM} up`: {UV_INSTALL_COMMAND}")
        return
    result = subprocess.run(UV_INSTALL_COMMAND, shell=True, stdin=subprocess.DEVNULL)  # noqa: S602 -- official installer, shown to the user verbatim
    if result.returncode == 0 and find_uv() is not None:
        success("uv installed.")
    else:
        warn(f"uv installation did not complete. Install it manually: {UV_INSTALL_COMMAND}")


def _disable_keys(config: dict[str, object], keys: list[str]) -> None:
    """Comment out the given keys in .env (only if currently configured) so the
    selected backend isn't overridden by a leftover key, and forget them for this
    run."""
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    for key in keys:
        if comment_out_env_key(ENV_PATH, key):
            success(f"Commented out {key} in {ENV_PATH.name}.")
        raw_env.pop(key, None)
        user_env.pop(key, None)


def report_configured_keys(config: dict[str, object]) -> None:
    """Print which brain keys are currently active in .env."""
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    active = [key for key in SECRET_ENV_KEYS if is_configured_secret_value(key, user_env.get(key))]
    if active:
        success(f"Keys set in {ENV_PATH.name}: {', '.join(active)}")
    else:
        warn(f"No brain keys set in {ENV_PATH.name}.")


BRAIN_BACKENDS = ("gemini", "innate", "none")


def apply_brain_backend(config: dict[str, object], backend: str, keys: str) -> None:
    """Write a choice someone already made, without asking again.

    The installer collects this before it installs anything, so the question
    lands in the first ten seconds rather than after apt, uv and a clone. It
    collects the answer only -- which key goes in .env, and which get commented
    out, stays here, so there is one implementation of that.

    ``keys`` is what the installer sent down the pipe: the brain key, then the
    optional Cartesia key on a second line.
    """
    brain_key, _, voice_key = keys.partition("\n")
    voice_key = voice_key.strip()
    if backend == "gemini":
        _save_key(config, GEMINI_API_KEY, brain_key)
        if voice_key:
            _save_key(config, CARTESIA_API_KEY, voice_key)
        _disable_keys(config, [INNATE_SERVICE_KEY])
    elif backend == "innate":
        _save_key(config, INNATE_SERVICE_KEY, brain_key)
        _disable_keys(config, [GEMINI_API_KEY, CARTESIA_API_KEY])
    else:
        _disable_keys(config, [GEMINI_API_KEY, CARTESIA_API_KEY, INNATE_SERVICE_KEY])
        warn("No brain backend selected. The sim will run without an agent.")
    report_configured_keys(config)


def configure_brain_backend(config: dict[str, object]) -> None:
    """Pick how the robot's brain reaches Gemini, and collect the matching key.

    The agent loop itself always runs on the robot (brain_client); the key only
    decides which way out it takes -- straight to Google with a Gemini key, or
    through the Innate proxy with a service key. Switching just uncomments the
    relevant key and comments out the others, so you can toggle back and forth
    without re-pasting. Non-interactively, just report what the robot will pick.
    """
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    has_gemini = is_configured_secret_value(GEMINI_API_KEY, user_env.get(GEMINI_API_KEY))
    has_service_key = is_configured_secret_value(INNATE_SERVICE_KEY, user_env.get(INNATE_SERVICE_KEY))

    if not is_interactive_terminal():
        if has_service_key:
            success("Innate proxy selected (INNATE_SERVICE_KEY detected).")
        elif has_gemini:
            success("Direct Gemini access selected (GEMINI_API_KEY detected).")
            if not is_configured_secret_value(CARTESIA_API_KEY, user_env.get(CARTESIA_API_KEY)):
                warn(f"No CARTESIA_API_KEY — the robot will run without speech. Add one to {ENV_PATH} for a voice.")
        else:
            warn(
                f"No brain key configured. Add GEMINI_API_KEY (your own Gemini key) or "
                f"INNATE_SERVICE_KEY (Innate proxy) to {ENV_PATH}."
            )
        report_configured_keys(config)
        return

    print()
    print(f"{CYAN}{BOLD}Cloud LLM Access{NC}")
    print(
        f"{DIM}The robot's agent runs on the robot, but thinks with a cloud LLM.\n"
        f"Choose how it reaches one:\n"
        f"  - Your own API keys: a Gemini key (https://aistudio.google.com/api-keys)\n"
        f"    for the agent, then an optional Cartesia key\n"
        f"    (https://play.cartesia.ai/keys) to give the robot a voice.\n"
        f"  - Innate service key (ships with a MARS robot): one key reaches both\n"
        f"    Gemini and Cartesia through Innate's proxy. Voice included.\n"
        f"  - None: drive, navigate, and trigger skills manually, with no agent.{NC}"
    )
    print()
    default_choice = "2" if has_service_key else "1"
    choice = _prompt_choice(
        "How would you like to access the cloud LLM?",
        {
            "1": "Your own API keys (Gemini, plus Cartesia for the robot's voice)",
            "2": "Innate service key (ships with a MARS robot)",
            "3": "None (run the sim without an agent)",
        },
        default=default_choice,
    )
    if choice == "1":
        _configure_own_keys(config)
        _disable_keys(config, [INNATE_SERVICE_KEY])
    elif choice == "2":
        _configure_key(config, INNATE_SERVICE_KEY, f"Paste {INNATE_SERVICE_KEY}")
        print(f"{GREEN}Innate proxy credentials are ready.{NC}")
        _disable_keys(config, [GEMINI_API_KEY, CARTESIA_API_KEY])
    else:
        _disable_keys(config, [GEMINI_API_KEY, CARTESIA_API_KEY, INNATE_SERVICE_KEY])
        warn("No brain backend selected. The sim will run without an agent.")

    report_configured_keys(config)
