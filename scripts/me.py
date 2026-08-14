#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The robot's own identity, as the Innate auth service sees it.

Two calls: the service key buys a JWT at /v1/auth, the JWT reads /v1/me. It lives apart
from its callers because it is a contract with the cloud — the endpoints, the bearer
shapes, the field names — and not a diagnostic.
"""

from __future__ import annotations

import json
import os
import urllib.request

DEFAULT_AUTH_URL = "https://auth-v1.svc.innate.bot"


def _request(url: str, bearer: str, data: bytes | None = None) -> dict:
    request = urllib.request.Request(
        url, data=data, headers={"Authorization": f"Bearer {bearer}", "User-Agent": "innate-robot"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _issuer() -> str:
    return os.environ.get("INNATE_AUTH_URL", DEFAULT_AUTH_URL).rstrip("/")


def auth_token(service_key: str) -> str:
    """Exchange the service key for a JWT, the same way auth_client does."""
    return _request(f"{_issuer()}/v1/auth", service_key, data=b"").get("token", "")


def fetch_identity(service_key: str) -> dict:
    """The robot record this service key belongs to: robot_id, hardware revision, colour."""
    return _request(f"{_issuer()}/v1/me", auth_token(service_key))


def robot_id(identity: dict) -> str | None:
    return identity.get("robot_id") or identity.get("id")
