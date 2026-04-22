"""OIDC robot authentication client.

Handles service-key → JWT exchange via OpenID Connect discovery,
with JWT timing introspection for proactive scheduled renewal.

Usage::

    from auth_client import AuthProvider

    auth = AuthProvider(issuer_url="https://auth-v1.innate.bot", service_key="sk_...")
    token = auth.token              # lazily discovers + fetches on first access
    print(auth.expires_at)          # datetime of JWT expiration

    # To force a renewal on the next access:
    auth.token_needs_renewal = True
    token = auth.token              # fetches a fresh JWT

with JWT timing introspection for proactive renewal scheduling.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Callable

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when authentication or token renewal fails."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code: int | None = status_code
        super().__init__(message)


def _is_transient_auth_error(exc: "AuthError") -> bool:
    """Return True when ``exc`` represents a retryable condition.

    Treated as transient:
      * HTTP 408 Request Timeout,
      * HTTP 429 Too Many Requests,
      * any HTTP 5xx server error,
      * no HTTP status *and* the AuthError was chained from a
        ``urllib.error.URLError`` (DNS not ready, TLS handshake,
        connection reset, timeout before any response).

    Everything else — notably 400/401/403/404, and semantic errors like
    "discovery response missing token_endpoint" or "auth response missing
    token" which raise ``AuthError`` with no ``status_code`` *and* no
    ``__cause__`` — is a permanent config/credential/server-contract
    issue and should fail fast so the caller (systemd unit, init
    script, ...) can surface it rather than spin forever.
    """
    status = exc.status_code
    if status is not None:
        if status in (408, 429):
            return True
        return 500 <= status < 600
    cause = exc.__cause__
    while cause is not None:
        if isinstance(cause, urllib.error.URLError):
            return True
        cause = cause.__cause__
    return False


def _decode_jwt_payload(token: str) -> dict[str, object]:
    """Decode the payload of a JWT *without* signature verification.

    Only used to extract timing claims (``iat``, ``exp``) from our own
    freshly-received tokens — never for trust decisions.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError(f"Malformed JWT: expected 3 parts, got {len(parts)}")

    payload_b64 = parts[1]
    # Restore padding stripped by JWT spec
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding

    raw: dict[str, object] = json.loads(base64.urlsafe_b64decode(payload_b64))
    return raw


class AuthProvider:
    """Exchanges an innate service key for a JWT and handles renewal.

    OIDC discovery is deferred until the first token request.  The
    provider also parses the JWT to extract ``iat`` and ``exp`` claims,
    enabling callers to schedule proactive renewal (e.g. at half-life).

    Thread-safe: concurrent access is serialised by an internal lock.
    """

    def __init__(self, issuer_url: str, service_key: str) -> None:
        self._issuer_url: str = issuer_url.rstrip("/")
        self._service_key: str = service_key
        self._token: str | None = None
        self._token_endpoint: str | None = None
        self._issued_at: datetime | None = None
        self._expires_at: datetime | None = None
        self._lock: threading.Lock = threading.Lock()
        self.token_needs_renewal: bool = True  # force fetch on first access

    # ── Public API ──────────────────────────────────────────────────

    @property
    def token(self) -> str:
        """Return the current JWT, discovering + fetching as needed."""
        if self.token_needs_renewal or self._token is None:
            return self._renew()
        return self._token

    @property
    def expires_at(self) -> datetime | None:
        """``exp`` claim of the current JWT, or ``None`` if not yet fetched."""
        return self._expires_at

    def seconds_until_renewal(self) -> float | None:
        """Seconds from now until the recommended renewal time (half-life).

        Returns ``None`` if no token has been fetched, or a negative
        value if renewal is overdue.
        """
        if self._issued_at is None or self._expires_at is None:
            return None
        half_life = (self._expires_at - self._issued_at) / 2
        renewal = self._issued_at + half_life
        return (renewal - datetime.now(timezone.utc)).total_seconds()

    def bearer_headers(self) -> dict[str, str]:
        """Return auth headers suitable for any HTTP or WS request.

        Refreshes the JWT if needed (sync, thread-safe).
        """
        return {
            "User-Agent": "innate-robot",
            "Authorization": f"Bearer {self.token}",
        }

    def wait_for_token(
        self,
        *,
        timeout: float | None = None,
        initial_delay: float = 2.0,
        max_delay: float = 30.0,
        on_retry: Callable[[int, "AuthError", float], None] | None = None,
    ) -> str:
        """Block until a JWT can be obtained, retrying transient failures.

        Intended for startup on systems where networking may not be
        immediately usable — e.g. cold boots where DNS hasn't come up
        yet (``URLError`` / ``EAI_AGAIN``) or boards without an RTC
        where TLS rejects the server cert as "not yet valid" until
        ``systemd-timesyncd`` has completed its first sync.  Both
        surface as :class:`AuthError` from
        :meth:`_discover_token_endpoint`.

        Uses exponential backoff between attempts.  Only *transient*
        :class:`AuthError` conditions are retried — permanent HTTP 4xx
        responses (wrong service key → 401, wrong issuer URL → 404,
        etc.) re-raise immediately so systemd can observe a clean
        failure and restart / report the misconfiguration instead of
        spinning forever.  Non-:class:`AuthError` exceptions also
        propagate immediately.

        Retried status codes:

        * ``None`` — no HTTP response (DNS, TLS, connection reset).
        * ``408`` Request Timeout.
        * ``429`` Too Many Requests.
        * ``5xx`` Server Error.

        Args:
            timeout: Max seconds to wait.  ``None`` means wait forever.
            initial_delay: First backoff delay in seconds.
            max_delay: Cap on the backoff delay.
            on_retry: Optional callback ``(attempt, error, next_delay_s)``
                invoked after each failure.  Useful to route progress
                messages to a non-stdlib logger (e.g. a ROS logger).
                When ``None``, warnings are emitted on the module logger.

        Returns:
            A valid JWT string.

        Raises:
            AuthError: If a permanent failure is seen, or if ``timeout``
                elapses without success.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None
        delay = initial_delay
        attempt = 0
        while True:
            attempt += 1
            try:
                return self.token
            except AuthError as exc:
                if not _is_transient_auth_error(exc):
                    raise
                # If there's no budget left for another sleep, raise now
                # instead of firing the callback with a meaningless
                # "retrying in 0s" and an immediate retry.
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise
                    next_delay = min(delay, remaining)
                else:
                    next_delay = delay
                if on_retry is not None:
                    on_retry(attempt, exc, next_delay)
                else:
                    logger.warning(
                        "Auth not ready (attempt %d): %s — retrying in %.0fs",
                        attempt,
                        exc,
                        next_delay,
                    )
                time.sleep(next_delay)
                delay = min(delay * 2, max_delay)

    async def ws_connect(self, url: str, extra_headers: dict[str, str] | None = None, **ws_kwargs):
        """Open an async WebSocket with Bearer auth and one 401 retry.

        Injects the current JWT as a ``Bearer`` header and retries once
        on a 401 response after forcing token renewal.

        Args:
            url: WebSocket URL (``wss://`` or ``ws://``).
            extra_headers: Additional headers merged after the auth header.
            **ws_kwargs: Forwarded to :func:`websockets.connect`
                (e.g. ``max_size``, ``ping_interval``, ``ping_timeout``).

        Returns:
            An open WebSocket connection (context-manageable).
        """
        import websockets  # optional dep

        for attempt in range(2):
            headers = self.bearer_headers()
            if extra_headers:
                headers.update(extra_headers)

            try:
                return await websockets.connect(
                    url,
                    additional_headers=headers,
                    **ws_kwargs,
                )
            except websockets.InvalidStatus as exc:
                if exc.response.status_code == 401 and attempt == 0:
                    logger.warning("WS auth 401 — renewing token and retrying")
                    self.token_needs_renewal = True
                    continue
                raise

        raise RuntimeError("WS auth retry exhausted")  # unreachable

    # ── Internals ───────────────────────────────────────────────────

    def _renew(self) -> str:
        """Discover (if needed) and fetch a fresh JWT."""
        with self._lock:
            # Double-check after acquiring the lock — another thread may
            # have already renewed while we were waiting.
            if not self.token_needs_renewal and self._token is not None:
                return self._token

            if self._token_endpoint is None:
                self._token_endpoint = self._discover_token_endpoint()

            logger.debug("Renewing JWT from %s", self._token_endpoint)
            try:
                req = urllib.request.Request(
                    self._token_endpoint,
                    data=b"",  # POST
                    headers={
                        "Authorization": f"Bearer {self._service_key}",
                        "User-Agent": "innate-robot",
                    },
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data: dict[str, object] = json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                raise AuthError(
                    f"Token exchange failed: HTTP {exc.code}",
                    status_code=exc.code,
                ) from exc
            except urllib.error.URLError as exc:
                raise AuthError(f"Token exchange request failed: {exc}") from exc

            jwt_token: str | None = data.get("token")
            if not jwt_token:
                raise AuthError(
                    "Auth response missing 'token' field; "
                    f"got keys: {list(data.keys())}",
                )

            # Parse timing claims from the freshly-received JWT
            self._parse_token_timing(jwt_token)

            self._token = jwt_token
            self.token_needs_renewal = False
            logger.info(
                "JWT renewed successfully (expires %s)",
                self._expires_at,
            )
            return jwt_token

    def _parse_token_timing(self, token: str) -> None:
        """Extract ``iat`` and ``exp`` from the JWT payload."""
        try:
            payload = _decode_jwt_payload(token)
            iat = payload.get("iat")
            exp = payload.get("exp")
            if isinstance(iat, (int, float)):
                self._issued_at = datetime.fromtimestamp(iat, tz=timezone.utc)
            else:
                self._issued_at = None
            if isinstance(exp, (int, float)):
                self._expires_at = datetime.fromtimestamp(exp, tz=timezone.utc)
            else:
                self._expires_at = None
        except Exception:
            logger.warning("Failed to parse JWT timing claims", exc_info=True)
            self._issued_at = None
            self._expires_at = None

    def _discover_token_endpoint(self) -> str:
        """Fetch the OIDC discovery document and extract the token endpoint.

        The returned path is rebased onto :attr:`_issuer_url` so the
        client works even when the discovery document advertises an
        internal hostname (e.g. ``http://auth:8080``).
        """
        url = f"{self._issuer_url}/.well-known/openid-configuration"
        logger.debug("Discovering OIDC config from %s", url)
        try:
            disco_req = urllib.request.Request(
                url, headers={"User-Agent": "innate-robot"}
            )
            with urllib.request.urlopen(disco_req, timeout=10) as resp:
                discovery: dict[str, object] = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            raise AuthError(
                f"OIDC discovery failed at {url}: HTTP {exc.code}",
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise AuthError(f"OIDC discovery failed at {url}: {exc}") from exc

        endpoint: str | None = discovery.get("token_endpoint")
        if not endpoint:
            raise AuthError(
                "OIDC discovery response missing 'token_endpoint'; "
                f"got keys: {list(discovery.keys())}",
            )

        # Rebase: keep only the path portion and prepend our issuer URL.
        from urllib.parse import urlparse

        path = urlparse(endpoint).path
        rebased = f"{self._issuer_url}{path}"
        logger.debug("Discovered token endpoint: %s (rebased to %s)", endpoint, rebased)
        return rebased
