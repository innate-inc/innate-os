# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Public sessions use an external credential relay, never owner credentials.

This guard catches deployment mistakes. It is not a Python sandbox: visitors
must be assumed able to inspect everything inside their session container.
The relay's network and tenant isolation is enforced by the cloud deployment.
"""

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

_CREDENTIAL_NAME = re.compile(
    r"(?:^|_)(?:API_KEY|SERVICE_KEY|ACCESS_KEY|PRIVATE_KEY|SECRET(?:_KEY)?|TOKEN|PASSWORD|CREDENTIALS?|DATABASE_URL|DSN|CONNECTION_STRING)(?:$|_)"
)


def public_demo_enabled() -> bool:
    return os.environ.get("INNATE_PUBLIC_DEMO", "").strip().lower() in {"1", "true", "yes"}


def demo_proxy_url() -> str:
    """Validate the credential-free public configuration without echoing values."""
    if any(value and _CREDENTIAL_NAME.search(name.upper()) for name, value in os.environ.items()):
        raise RuntimeError("Public simulator must not contain credential environment variables")
    for value in os.environ.values():
        if "://" not in value:
            continue
        try:
            url = urlsplit(value)
            if url.username or url.password:
                raise RuntimeError("Public simulator must not contain credentials in environment URLs")
        except ValueError:
            pass  # Not a URL; never echo arbitrary environment text in diagnostics.
    raw = os.environ.get("INNATE_DEMO_PROXY_URL", "").rstrip("/")
    try:
        parsed = urlsplit(raw)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.path in {"", "/"}
        )
    except ValueError:
        valid = False
    if not valid:
        raise RuntimeError("Public simulator requires an explicit credential-free relay URL")
    return raw


def check_runtime() -> None:
    """Fail before any demo service starts if owner configuration was included."""
    if not public_demo_enabled():
        raise RuntimeError("Public simulator requires INNATE_PUBLIC_DEMO=1")
    demo_proxy_url()
    root = Path(os.environ.get("INNATE_OS_ROOT", str(Path.home() / "innate-os")))
    # Refuse the whole files instead of parsing key names and missing an alias.
    if any(p.exists() for p in (root / ".env", Path("/etc/innate.env"), root / "config" / "settings.yaml")):
        raise RuntimeError("Public simulator must not contain owner environment or settings files")


if __name__ == "__main__":
    check_runtime()
