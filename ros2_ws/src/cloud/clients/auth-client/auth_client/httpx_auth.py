"""httpx ``Auth`` flow with automatic 401 JWT renewal.

Drop-in replacement for manual token management.  Attach to any
``httpx.Client`` or ``httpx.AsyncClient`` and every request will carry
a valid Bearer token — renewed transparently on 401.

Usage::

    from auth_client import AuthProvider
    from auth_client.httpx_auth import InnateBearerAuth

    provider = AuthProvider(issuer_url="...", service_key="sk_...")
    auth = InnateBearerAuth(provider)

    client = httpx.Client(auth=auth)
    resp = client.get("https://...")    # token added automatically
    # On 401 → token is renewed and request retried once.
"""

from __future__ import annotations

from typing import Generator

import httpx

from auth_client.provider import AuthProvider


class InnateBearerAuth(httpx.Auth):
    """httpx Auth flow that attaches an Innate JWT and retries on 401."""

    def __init__(self, provider: AuthProvider) -> None:
        self._provider = provider

    # -- Sync flow (httpx.Client) ---------------------------------------------

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._provider.token}"
        response = yield request

        if response.status_code == 401:
            self._provider.token_needs_renewal = True
            request.headers["Authorization"] = f"Bearer {self._provider.token}"
            yield request
