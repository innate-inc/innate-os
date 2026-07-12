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
    CLOUD_AGENT_DIR_NAME,
    CLOUD_AGENT_GIT_URL,
    ENV_PATH,
    GEMINI_API_KEY,
    SECRET_ENV_KEYS,
    WORKSPACE_ROOT,
    is_configured_secret_value,
    log,
    success,
    warn,
)
from dashboard import BOLD, CYAN, DIM, GREEN, NC, YELLOW
from runtime import (
    UV_INSTALL_COMMAND,
    cloud_agent_checkout_pinned,
    cloud_agent_git_status,
    cloud_agent_lock,
    find_uv,
)

INNATE_SERVICE_KEY = "INNATE_SERVICE_KEY"


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
    try:
        return getpass.getpass(f"{YELLOW}{question}: {NC}", stream=sys.stdout).strip()
    except (KeyboardInterrupt, EOFError):
        print()
        raise SystemExit(1)  # noqa: B904


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


def _save_gemini_key(config: dict[str, object], gemini_key: str) -> None:
    write_env_value(ENV_PATH, GEMINI_API_KEY, gemini_key)
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    raw_env[GEMINI_API_KEY] = gemini_key
    user_env[GEMINI_API_KEY] = gemini_key
    success(f"Saved {GEMINI_API_KEY} to {ENV_PATH}.")


def _configure_gemini_key(config: dict[str, object]) -> None:
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    if is_configured_secret_value(GEMINI_API_KEY, user_env.get(GEMINI_API_KEY)):
        success(f"{GEMINI_API_KEY} already configured.")
        return

    restored = uncomment_env_key(ENV_PATH, GEMINI_API_KEY)
    if restored is not None:
        raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
        raw_env[GEMINI_API_KEY] = restored
        user_env[GEMINI_API_KEY] = restored
        success(f"Re-enabled {GEMINI_API_KEY} in {ENV_PATH.name}.")
        return

    shell_value = os.environ.get(GEMINI_API_KEY, "").strip()
    if is_configured_secret_value(GEMINI_API_KEY, shell_value) and _prompt_yes_no(
        f"Found {GEMINI_API_KEY} in your shell. Save it to {ENV_PATH.name}?", default=True
    ):
        _save_gemini_key(config, shell_value)
        return

    while True:
        gemini_key = _prompt_secret(f"Paste {GEMINI_API_KEY}")
        if is_configured_secret_value(GEMINI_API_KEY, gemini_key):
            _save_gemini_key(config, gemini_key)
            return
        warn("Gemini key cannot be empty. Press Ctrl+C to cancel.")


def _configure_service_key(config: dict[str, object]) -> None:
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    if is_configured_secret(user_env.get(INNATE_SERVICE_KEY)):
        success(f"{INNATE_SERVICE_KEY} already configured.")
        return

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
            print(f"{GREEN}Hosted brain credentials are ready.{NC}")
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


def ensure_cloud_agent_repo(config: dict[str, object]) -> None:
    """Make the local cloud-agent source match sim/cloud-agent.lock -- the
    revision this innate-os is tested against. A fresh clone lands on the
    pin; an existing checkout on any other commit gets a warning and an
    offer to align (never forced: forks and deliberate experiments answer
    "no" and are left alone)."""
    lock = cloud_agent_lock(config)

    path: Path | None = None
    existing = config.get("cloud_repo")
    if existing and Path(str(existing)).exists():
        path = Path(str(existing))
    elif (WORKSPACE_ROOT / CLOUD_AGENT_DIR_NAME).exists():
        path = WORKSPACE_ROOT / CLOUD_AGENT_DIR_NAME

    if path is not None:
        status = cloud_agent_git_status(path)
        if status is None or lock is None:
            success(f"Cloud-agent source present at {path}.")
            return
        if not status["official"]:
            log(f"Using custom cloud-agent source at {path} ({status['short']}).")
            return
        if status["sha"] == lock["commit"]:
            success(f"Cloud-agent source present at {path} (tested revision {status['short']}).")
            return
        warn(
            f"Cloud-agent at {path} is at {status['short']}; this innate-os is tested "
            f"against {lock['commit'][:9]}. Other revisions may crash the agent."
        )
        if status["dirty"]:
            warn(
                f"Checkout has local changes -- align manually: git -C {path} stash && git -C {path} checkout {lock['commit'][:9]}"
            )
            return
        if is_interactive_terminal() and _prompt_yes_no("Check out the tested revision now?", default=True):
            if cloud_agent_checkout_pinned(path, lock["commit"]):
                success(f"Cloud-agent aligned to {lock['commit'][:9]}.")
            else:
                warn(
                    f"Could not check out {lock['commit'][:9]} -- align manually with git -C {path} fetch && git -C {path} checkout {lock['commit'][:9]}."
                )
        else:
            warn("Keeping the current revision.")
        return

    target = WORKSPACE_ROOT / CLOUD_AGENT_DIR_NAME
    log(f"Cloning innate-cloud-agent into {target}...")
    result = subprocess.run(
        ["git", "clone", CLOUD_AGENT_GIT_URL, str(target)],
        stdin=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        warn(
            f"Could not clone {CLOUD_AGENT_GIT_URL}. Clone it manually to {target} "
            "(needs GitHub SSH access to innate-inc)."
        )
        return
    if lock is not None and not cloud_agent_checkout_pinned(target, lock["commit"]):
        warn(f"Cloned, but could not check out the tested revision {lock['commit'][:9]}; using the default branch.")
    success(f"Cloned innate-cloud-agent to {target}" + (f" (tested revision {lock['commit'][:9]})." if lock else "."))


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


def configure_brain_backend(config: dict[str, object]) -> None:
    """Pick the brain backend and collect the matching key.

    Local needs a Gemini key (and the cloud-agent source); hosted needs an Innate
    service key; none runs the sim without an agent. Switching backends just
    uncomments the relevant key and comments out the others, so you can toggle
    back and forth without re-pasting. Non-interactively, just report what
    auto-detection will pick.
    """
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    has_gemini = is_configured_secret_value(GEMINI_API_KEY, user_env.get(GEMINI_API_KEY))
    has_service_key = is_configured_secret(user_env.get(INNATE_SERVICE_KEY))

    if not is_interactive_terminal():
        if has_gemini:
            ensure_cloud_agent_repo(config)
            success("Local brain selected (GEMINI_API_KEY detected).")
        elif has_service_key:
            success("Hosted brain selected (INNATE_SERVICE_KEY detected).")
        else:
            warn(
                "No brain backend configured. Add GEMINI_API_KEY (local brain) or "
                f"INNATE_SERVICE_KEY (hosted) to {ENV_PATH}."
            )
        report_configured_keys(config)
        return

    print()
    print(f"{CYAN}{BOLD}Brain Backend{NC}")
    print(
        f"{DIM}The brain is the robot's agent. Run it locally with a Gemini key, "
        f"use Innate's hosted brain with a service key, or run the sim with no agent.{NC}"
    )
    print()
    default_choice = "1" if has_gemini else ("2" if has_service_key else "3")
    choice = _prompt_choice(
        "Which brain backend?",
        {
            "1": "Local cloud-agent (Gemini key: get from https://aistudio.google.com/api-keys)",
            "2": "Hosted Innate brain (service key)",
            "3": "None (run the sim without an agent)",
        },
        default=default_choice,
    )
    if choice == "1":
        _configure_gemini_key(config)
        ensure_cloud_agent_repo(config)
        _disable_keys(config, [INNATE_SERVICE_KEY])
    elif choice == "2":
        _configure_service_key(config)
        _disable_keys(config, [GEMINI_API_KEY])
    else:
        _disable_keys(config, [GEMINI_API_KEY, INNATE_SERVICE_KEY])
        warn("No brain backend selected. The sim will run without an agent.")

    report_configured_keys(config)
