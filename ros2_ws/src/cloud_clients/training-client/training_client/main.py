"""
Minimal entry point — just wires up a SkillManager.

Import and use in your own scripts::

    from training_client.main import create_manager
    manager = create_manager("https://server.example.com", "my-token")
"""

from __future__ import annotations

from typing import Any

from .src.skill_manager import SkillManager
from .src.types import ClientConfig, DEFAULT_AUTH_ISSUER_URL


def create_manager(
    server_url: str,
    auth_token: str,
    auth_issuer_url: str = DEFAULT_AUTH_ISSUER_URL,
    **config_overrides: Any,
) -> SkillManager:
    """Create a :class:`SkillManager` with the given server URL and auth token.

    *auth_token* is treated as a service key and exchanged for a JWT via
    OIDC discovery at *auth_issuer_url*.  The JWT is automatically renewed
    on 401 ``invalid_token`` responses (RFC 6750).

    Set *auth_issuer_url* to ``""`` to skip OIDC and use *auth_token* as a
    plain bearer token (development only).
    """
    config = ClientConfig(
        server_url=server_url,
        auth_token=auth_token,
        auth_issuer_url=auth_issuer_url,
        **config_overrides,
    )
    return SkillManager(config)
