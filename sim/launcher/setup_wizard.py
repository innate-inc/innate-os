# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

from config import (
    BRAIN_BACKEND,
    CLI_SIM,
    ENV_PATH,
    GEMINI_API_KEY,
    GEMINI_BACKEND,
    INNATE_BACKEND,
    INNATE_SERVICE_KEY,
    NO_BACKEND,
    OPENAI_API_KEY,
    OPENAI_BACKEND,
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


def is_configured_secret(value: str | None) -> bool:
    return is_configured_secret_value(INNATE_SERVICE_KEY, value)


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


def _save_service_key(config: dict[str, object], service_key: str) -> None:
    write_env_value(ENV_PATH, INNATE_SERVICE_KEY, service_key)
    _use_service_key_for_run(config, service_key)
    success(f"Saved {INNATE_SERVICE_KEY} to {ENV_PATH}.")


def _use_service_key_for_run(config: dict[str, object], service_key: str) -> None:
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    raw_env[INNATE_SERVICE_KEY] = service_key
    user_env[INNATE_SERVICE_KEY] = service_key


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


def _save_vendor_key(config: dict[str, object], env_key: str, value: str) -> None:
    write_env_value(ENV_PATH, env_key, value)
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    raw_env[env_key] = value
    user_env[env_key] = value
    success(f"Saved {env_key} to {ENV_PATH}.")


def _configure_vendor_key(config: dict[str, object], env_key: str) -> None:
    """Collect one vendor's own API key (Gemini or OpenAI) into .env."""
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    if is_configured_secret_value(env_key, user_env.get(env_key)):
        if not _prompt_yes_no(f"{env_key} is already set. Replace it?", default=False):
            return
    else:
        restored = uncomment_env_key(ENV_PATH, env_key)
        if restored is not None:
            raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
            raw_env[env_key] = restored
            user_env[env_key] = restored
            success(f"Re-enabled {env_key} in {ENV_PATH.name}.")
            return

        shell_value = os.environ.get(env_key, "").strip()
        if is_configured_secret_value(env_key, shell_value) and _prompt_yes_no(
            f"Found {env_key} in your shell. Save it to {ENV_PATH.name}?", default=True
        ):
            _save_vendor_key(config, env_key, shell_value)
            return

    while True:
        value = _prompt_secret(f"Paste {env_key}")
        if is_configured_secret_value(env_key, value):
            _save_vendor_key(config, env_key, value)
            return
        warn(f"{env_key} cannot be empty. Press Ctrl+C to cancel.")


def _configure_service_key(config: dict[str, object]) -> None:
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    if is_configured_secret(user_env.get(INNATE_SERVICE_KEY)):
        if not _prompt_yes_no(f"{INNATE_SERVICE_KEY} is already set. Replace it?", default=False):
            return
    else:
        restored = uncomment_env_key(ENV_PATH, INNATE_SERVICE_KEY)
        if restored is not None:
            _use_service_key_for_run(config, restored)
            success(f"Re-enabled {INNATE_SERVICE_KEY} in {ENV_PATH.name}.")
            return

        shell_value = os.environ.get(INNATE_SERVICE_KEY, "").strip()
        if is_configured_secret(shell_value) and _prompt_yes_no(
            f"Found {INNATE_SERVICE_KEY} in your shell. Save it to {ENV_PATH.name}?", default=True
        ):
            _save_service_key(config, shell_value)
            return

    while True:
        service_key = _prompt_secret(f"Paste {INNATE_SERVICE_KEY}")
        if is_configured_secret(service_key):
            _save_service_key(config, service_key)
            print(f"{GREEN}Innate proxy credentials are ready.{NC}")
            return
        warn("Service key cannot be empty. Press Ctrl+C to cancel.")


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


BRAIN_BACKENDS = (GEMINI_BACKEND, OPENAI_BACKEND, INNATE_BACKEND, NO_BACKEND)


def apply_brain_backend(config: dict[str, object], backend: str, key: str) -> None:
    """Write a choice someone already made, without asking again.

    The installer collects this before it installs anything, so the question
    lands in the first ten seconds rather than after apt, uv and a clone. It
    collects the answer only -- which key goes in .env, and which get commented
    out, stays here, so there is one implementation of that.
    """
    if backend in _VENDOR_KEYS:
        _save_vendor_key(config, _VENDOR_KEYS[backend], key)
        _select_provider(config, backend)
        _disable_keys(config, [INNATE_SERVICE_KEY, *_other_vendor_keys(backend)])
    elif backend == INNATE_BACKEND:
        # The proxy serves every provider, so the choice of provider is left
        # wherever it already stands.
        _save_service_key(config, key)
        _disable_keys(config, list(_VENDOR_KEYS.values()))
    else:
        _disable_keys(config, [*_VENDOR_KEYS.values(), INNATE_SERVICE_KEY])
        warn("No brain backend selected. The sim will run without an agent.")
    report_configured_keys(config)


_VENDOR_KEYS = {GEMINI_BACKEND: GEMINI_API_KEY, OPENAI_BACKEND: OPENAI_API_KEY}


def _other_vendor_keys(backend: str) -> list[str]:
    return [key for name, key in _VENDOR_KEYS.items() if name != backend]


def _select_provider(config: dict[str, object], backend: str) -> None:
    """Point the robot's brain at this vendor. A vendor key only works for its
    own provider, so choosing the key has to choose the provider with it."""
    write_env_value(ENV_PATH, BRAIN_BACKEND, backend)
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    raw_env[BRAIN_BACKEND] = backend
    user_env[BRAIN_BACKEND] = backend


def configure_brain_backend(config: dict[str, object]) -> None:
    """Pick how the robot's brain reaches its model, and collect the matching key.

    The agent loop itself always runs on the robot (brain_client); the key only
    decides which way out it takes -- straight to a vendor with that vendor's
    key, or through the Innate proxy with a service key. Switching just
    uncomments the relevant key and comments out the others, so you can toggle
    back and forth without re-pasting. Non-interactively, just report what the
    robot will pick.
    """
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    has_gemini = is_configured_secret_value(GEMINI_API_KEY, user_env.get(GEMINI_API_KEY))
    has_openai = is_configured_secret_value(OPENAI_API_KEY, user_env.get(OPENAI_API_KEY))
    has_service_key = is_configured_secret(user_env.get(INNATE_SERVICE_KEY))

    if not is_interactive_terminal():
        if has_service_key:
            success("Innate proxy selected (INNATE_SERVICE_KEY detected).")
        elif has_gemini:
            success("Direct Gemini access selected (GEMINI_API_KEY detected).")
        elif has_openai:
            success("Direct OpenAI access selected (OPENAI_API_KEY detected).")
        else:
            warn(
                f"No brain key configured. Add GEMINI_API_KEY or OPENAI_API_KEY (your own vendor "
                f"key) or INNATE_SERVICE_KEY (Innate proxy) to {ENV_PATH}."
            )
        report_configured_keys(config)
        return

    print()
    print(f"{CYAN}{BOLD}Cloud LLM Access{NC}")
    print(
        f"{DIM}The robot's agent runs on the robot, but thinks with a cloud LLM.\n"
        f"Choose how it reaches one:\n"
        f"  - Your own Gemini or OpenAI key: the agent calls that vendor directly.\n"
        f"    Everything works except voice.\n"
        f"  - Innate service key (ships with a MARS robot): the agent calls the model\n"
        f"    through Innate's proxy. Full experience, including the robot's voice.\n"
        f"  - None: drive, navigate, and trigger skills manually, with no agent.{NC}"
    )
    print()
    default_choice = "3" if has_service_key else "1"
    choice = _prompt_choice(
        "How would you like to access the cloud LLM?",
        {
            "1": "Your own Gemini key (get one at https://aistudio.google.com/api-keys)",
            "2": "Your own OpenAI key (get one at https://platform.openai.com/api-keys)",
            "3": "Innate service key (from your robot)",
            "4": "None (run the sim without an agent)",
        },
        default=default_choice,
    )
    if choice in ("1", "2"):
        backend = GEMINI_BACKEND if choice == "1" else OPENAI_BACKEND
        _configure_vendor_key(config, _VENDOR_KEYS[backend])
        _select_provider(config, backend)
        _disable_keys(config, [INNATE_SERVICE_KEY, *_other_vendor_keys(backend)])
    elif choice == "3":
        _configure_service_key(config)
        _disable_keys(config, list(_VENDOR_KEYS.values()))
    else:
        _disable_keys(config, [*_VENDOR_KEYS.values(), INNATE_SERVICE_KEY])
        warn("No brain backend selected. The sim will run without an agent.")

    report_configured_keys(config)
