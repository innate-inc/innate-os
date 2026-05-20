from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

from config import ENV_PATH, HOSTED_MODE, warn, success
from dashboard import BOLD, CYAN, DIM, GREEN, NC, YELLOW

INNATE_SERVICE_KEY = "INNATE_SERVICE_KEY"
SERVICE_KEY_PLACEHOLDERS = {
    "",
    "your_service_key_here",
    "your-innate-service-key",
    "your-generated-token-here",
}


def is_interactive_terminal() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def is_configured_secret(value: str | None) -> bool:
    if value is None:
        return False
    stripped = value.strip()
    return stripped not in SERVICE_KEY_PLACEHOLDERS


def _prompt_yes_no(question: str, *, default: bool = False) -> bool:
    default_label = "Y/n" if default else "y/N"
    while True:
        try:
            value = input(f"{YELLOW}{question} [{default_label}]: {NC}").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            raise SystemExit(1)
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print(f"{YELLOW}Please enter y or n.{NC}")


def _prompt_secret(question: str) -> str:
    try:
        return getpass.getpass(f"{YELLOW}{question}: {NC}").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        raise SystemExit(1)


def write_env_value(path: Path, key: str, value: str) -> None:
    if "\n" in value or "\r" in value:
        raise ValueError(f"{key} cannot contain newlines")

    replacement = f"{key}={value}"
    lines = path.read_text().splitlines() if path.exists() else []
    updated = False
    output: list[str] = []

    for line in lines:
        stripped = line.strip()
        uncommented = stripped[1:].strip() if stripped.startswith("#") else stripped
        if uncommented.startswith(f"{key}="):
            if not updated:
                output.append(replacement)
                updated = True
            else:
                output.append(line)
        else:
            output.append(line)

    if not updated:
        if output and output[-1].strip():
            output.append("")
        output.append(replacement)

    path.write_text("\n".join(output) + "\n")


def _save_service_key(config: dict[str, object], service_key: str) -> None:
    write_env_value(ENV_PATH, INNATE_SERVICE_KEY, service_key)
    raw_env: dict[str, str] = config["raw_env"]  # type: ignore[assignment]
    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    raw_env[INNATE_SERVICE_KEY] = service_key
    user_env[INNATE_SERVICE_KEY] = service_key
    success(f"Saved {INNATE_SERVICE_KEY} to {ENV_PATH}.")


def configure_hosted_service_key(config: dict[str, object]) -> None:
    if config["mode"] != HOSTED_MODE:
        return

    user_env: dict[str, str] = config["user_env"]  # type: ignore[assignment]
    if is_configured_secret(user_env.get(INNATE_SERVICE_KEY)):
        return

    shell_value = os.environ.get(INNATE_SERVICE_KEY, "").strip()
    if is_configured_secret(shell_value):
        if not is_interactive_terminal():
            success(f"Using {INNATE_SERVICE_KEY} from the current shell.")
            return
        if _prompt_yes_no(
            f"Found {INNATE_SERVICE_KEY} in your shell. Save it to {ENV_PATH.name}?",
            default=True,
        ):
            _save_service_key(config, shell_value)
        else:
            success(f"Using {INNATE_SERVICE_KEY} from the current shell for this run.")
        return

    if not is_interactive_terminal():
        warn(
            f"Hosted brain mode needs {INNATE_SERVICE_KEY}. "
            f"Add it to {ENV_PATH} or switch sim/config.toml cloud_agent.mode to local-source."
        )
        return

    print()
    print(f"{CYAN}{BOLD}Hosted Brain Backend{NC}")
    print(
        f"{DIM}Hosted mode connects the simulator to Innate's hosted brain. "
        f"It needs an Innate service key saved in {ENV_PATH.name}.{NC}"
    )
    print()

    if not _prompt_yes_no("Do you have an Innate service key?", default=False):
        warn(
            f"Skipping hosted brain credentials. Add {INNATE_SERVICE_KEY} to {ENV_PATH} later, "
            "or use local-source cloud agent mode."
        )
        return

    while True:
        service_key = _prompt_secret(f"Paste {INNATE_SERVICE_KEY}")
        if is_configured_secret(service_key):
            _save_service_key(config, service_key)
            print(f"{GREEN}Hosted brain credentials are ready.{NC}")
            return
        warn("That does not look like a service key. Press Ctrl+C to cancel.")
