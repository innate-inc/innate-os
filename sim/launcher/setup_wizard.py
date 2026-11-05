# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from __future__ import annotations

import getpass
import os
import subprocess
import sys
from pathlib import Path

from config import (
    CLI_SIM,
    DEFAULT_LLM_MODEL,
    ENV_PATH,
    GEMINI_API_KEY,
    INNATE_SERVICE_KEY,
    SECRET_ENV_KEYS,
    SETTINGS_PATH,
    VENDOR_API_KEYS,
    VENDOR_ENV_KEYS,
    is_configured_secret_value,
    llm_vendor,
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


def _save_vendor_key(config: dict[str, object], key_name: str, vendor_key: str) -> None:
    write_env_value(ENV_PATH, key_name, vendor_key)
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    raw_env[key_name] = vendor_key
    user_env[key_name] = vendor_key
    success(f"Saved {key_name} to {ENV_PATH}.")


def _configure_vendor_key(config: dict[str, object], key_name: str) -> None:
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    if is_configured_secret_value(key_name, user_env.get(key_name)):
        if not _prompt_yes_no(f"{key_name} is already set. Replace it?", default=False):
            return
    else:
        restored = uncomment_env_key(ENV_PATH, key_name)
        if restored is not None:
            raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
            raw_env[key_name] = restored
            user_env[key_name] = restored
            success(f"Re-enabled {key_name} in {ENV_PATH.name}.")
            return

        shell_value = os.environ.get(key_name, "").strip()
        if is_configured_secret_value(key_name, shell_value) and _prompt_yes_no(
            f"Found {key_name} in your shell. Save it to {ENV_PATH.name}?", default=True
        ):
            _save_vendor_key(config, key_name, shell_value)
            return

    while True:
        vendor_key = _prompt_secret(f"Paste {key_name}")
        if is_configured_secret_value(key_name, vendor_key):
            _save_vendor_key(config, key_name, vendor_key)
            return
        warn("API key cannot be empty. Press Ctrl+C to cancel.")


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
    active = [key for key in (*SECRET_ENV_KEYS, *VENDOR_ENV_KEYS) if is_configured_secret_value(key, user_env.get(key))]
    if active:
        success(f"Keys set in {ENV_PATH.name}: {', '.join(active)}")
    else:
        warn(f"No brain keys set in {ENV_PATH.name}.")


BRAIN_BACKENDS = ("gemini", "openai", "innate", "none")


def _select_vendor_model(config: dict[str, object], backend: str) -> None:
    """Set the launch default; explicit Settings overrides remain the user's."""
    vendor = "google" if backend == "gemini" else "openai"
    default = DEFAULT_LLM_MODEL if backend == "gemini" else "openai:gpt-5.4-mini"
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    current = user_env.get("LLM_MODEL", "")
    model = current if current and llm_vendor(current) == vendor else default
    write_env_value(ENV_PATH, "LLM_MODEL", model)
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    raw_env["LLM_MODEL"] = model
    user_env["LLM_MODEL"] = model
    success(f"Default model: {model}.")
    print("In the web app, open Settings → Agent → Model to select or change the model.")
    print(f"A model already saved in {SETTINGS_PATH} takes precedence over this default.")


def _finish_backend_selection(config: dict[str, object], backend: str) -> None:
    if backend in ("gemini", "openai"):
        _disable_keys(config, [INNATE_SERVICE_KEY])
        _select_vendor_model(config, backend)
    elif backend == "innate":
        _disable_keys(config, [GEMINI_API_KEY, "OPENAI_API_KEY"])
    else:
        _disable_keys(config, [INNATE_SERVICE_KEY, *dict.fromkeys(VENDOR_API_KEYS.values()), "LLM_API_KEY"])
        warn("No brain backend selected. The sim will run without an agent.")
    report_configured_keys(config)
    print(f"If the simulator is running, restart it with `{CLI_SIM} down` then `{CLI_SIM} up`.")


def apply_brain_backend(config: dict[str, object], backend: str, key: str) -> None:
    """Write a choice someone already made, without asking again.

    The installer collects this before it installs anything, so the question
    lands in the first ten seconds rather than after apt, uv and a clone. It
    collects the answer only -- which key goes in .env, and which get commented
    out, stays here, so there is one implementation of that.
    """
    if backend not in BRAIN_BACKENDS:
        raise ValueError(f"Unknown brain backend: {backend}")
    if backend != "none" and not is_configured_secret_value(
        INNATE_SERVICE_KEY if backend == "innate" else VENDOR_API_KEYS["google" if backend == "gemini" else backend],
        key,
    ):
        raise ValueError("API key cannot be empty or a placeholder")
    if backend in ("gemini", "openai"):
        _save_vendor_key(config, GEMINI_API_KEY if backend == "gemini" else "OPENAI_API_KEY", key)
    elif backend == "innate":
        _save_service_key(config, key)
    _finish_backend_selection(config, backend)


def configure_brain_backend(config: dict[str, object]) -> None:
    """Choose a provider and collect its key, preserving keys for later reuse."""
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    has_gemini = is_configured_secret_value(GEMINI_API_KEY, user_env.get(GEMINI_API_KEY))
    has_openai = is_configured_secret_value("OPENAI_API_KEY", user_env.get("OPENAI_API_KEY"))
    has_service_key = is_configured_secret(user_env.get(INNATE_SERVICE_KEY))

    if not is_interactive_terminal():
        if has_service_key:
            success("Innate proxy selected (INNATE_SERVICE_KEY detected).")
        elif has_openai:
            success("OpenAI key detected. Select an OpenAI model in Settings → Agent → Model.")
        elif has_gemini:
            success("Direct Gemini access selected (GEMINI_API_KEY detected).")
        else:
            warn(
                f"No brain key configured. Add OPENAI_API_KEY, GEMINI_API_KEY, or "
                f"INNATE_SERVICE_KEY (Innate proxy) to {ENV_PATH}."
            )
        report_configured_keys(config)
        return

    print()
    print(f"{CYAN}{BOLD}Cloud LLM Access{NC}")
    print(
        f"{DIM}The robot's agent runs on the robot, but thinks with a cloud LLM.\n"
        f"Choose how it reaches one:\n"
        f"  - Your own Gemini key: the agent calls Google directly. Everything\n"
        f"    works except voice.\n"
        f"  - Your own OpenAI key: the agent calls OpenAI directly. No voice.\n"
        f"  - Innate service key (ships with a MARS robot): Gemini or OpenAI\n"
        f"    through Innate's proxy. Full experience, including the robot's voice.\n"
        f"  - None: drive, navigate, and trigger skills manually, with no agent.{NC}"
    )
    print()
    default_choice = "3" if has_service_key else "2" if has_openai and not has_gemini else "1"
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
    backend = {"1": "gemini", "2": "openai", "3": "innate", "4": "none"}[choice]
    if backend in ("gemini", "openai"):
        _configure_vendor_key(config, GEMINI_API_KEY if backend == "gemini" else "OPENAI_API_KEY")
    elif backend == "innate":
        _configure_service_key(config)
    _finish_backend_selection(config, backend)
