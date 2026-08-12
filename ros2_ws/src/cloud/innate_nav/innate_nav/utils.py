#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pure helpers for the innate_nav node."""

from __future__ import annotations

DEFAULT_PORT = 8900


def server_url(value: str, fallback: str) -> str:
    """Turn what a human typed into a base URL for the policy client.

    Accepts a bare address ("10.0.0.4"), an address with a port, or a full URL;
    empty falls back to the node's configured server. The scheme survives
    untouched because it is what decides ws:// versus wss:// downstream, so an
    explicit https:// is how the stream gets encrypted.
    """
    text = value.strip().rstrip("/")
    if not text:
        return fallback
    if "://" in text:
        return text
    return f"http://{_with_port(text)}"


def _with_port(authority: str) -> str:
    if authority.startswith("["):
        return authority if "]:" in authority else f"{authority}:{DEFAULT_PORT}"
    host, sep, port = authority.rpartition(":")
    if sep and port.isdigit() and ":" not in host:
        return authority
    # A bare IPv6 literal keeps its own colons; only brackets make a port
    # unambiguous after them.
    if ":" in authority:
        return f"[{authority}]:{DEFAULT_PORT}"
    return f"{authority}:{DEFAULT_PORT}"
