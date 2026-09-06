# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Local CLI credential entry; deliberately has no HTTP or ROS interface."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
from env_store import write_env_value

PROVIDERS = {
    "openai": "OPENAI_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "innate": "INNATE_SERVICE_KEY",
}


def _require_owner_runtime() -> None:
    if os.environ.get("INNATE_PUBLIC_DEMO") == "1":
        raise click.ClickException("API key configuration is disabled in the public simulator.")


def _write(root: Path, name: str, value: str) -> None:
    try:
        write_env_value(root / ".env", name, value, allow_empty=not value)
    except (OSError, ValueError):
        # Do not interpolate exceptions; callers can supply arbitrary input.
        raise click.ClickException("Could not save key. Use a single API key and a writable local .env file.") from None


def set_key(root: Path, provider: str, *, from_stdin: bool = False) -> None:
    _require_owner_runtime()
    name = PROVIDERS[provider]
    if from_stdin:
        value = sys.stdin.read(8193).removesuffix("\n").removesuffix("\r")
    else:
        # Click fails closed if hidden input is unavailable; never echo a key
        # through getpass's fallback for an unattended command.
        if not sys.stdin.isatty():
            raise click.ClickException("Use --stdin with a secret-manager pipe, or run from a terminal.")
        value = click.prompt(name, hide_input=True, err=True)
    if not value.strip() or len(value) > 8192 or value != value.strip():
        raise click.ClickException("Expected one nonempty API key without surrounding whitespace.")
    _write(root, name, value)
    click.echo(f"Saved {name}. Restart the robot nodes (innate restart) or restart your local simulator to apply.")
    click.echo("A configured Innate service route takes precedence; adding a key does not change the selected model.")


def remove_key(root: Path, provider: str) -> None:
    _require_owner_runtime()
    name = PROVIDERS[provider]
    # Blank overrides inherited shell/system values on the next launch. Deleting
    # the line would silently re-enable a key from /etc/innate.env.
    _write(root, name, "")
    click.echo(f"Cleared {name}. Restart the robot nodes or local simulator to apply.")


def status(root: Path) -> None:
    _require_owner_runtime()
    from config import parse_env_file

    try:
        values = {**os.environ, **parse_env_file(Path("/etc/innate.env")), **parse_env_file(root / ".env")}
    except OSError:
        raise click.ClickException("Could not read local key configuration.") from None
    for provider, name in PROVIDERS.items():
        click.echo(f"{provider}: {'configured' if values.get(name, '').strip() else 'not configured'}")
    click.echo("Configuration presence only; provider access is checked when used. Restart after changes.")
