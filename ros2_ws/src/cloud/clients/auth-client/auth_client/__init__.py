"""Innate auth client: OIDC JWT acquisition and timed renewal for robots.

Usage::

    from auth_client import AuthProvider, AuthError

    auth = AuthProvider(
        issuer_url="https://auth-v1.innate.bot",
        service_key="sk_...",
    )
    token = auth.token          # lazily discovers OIDC + fetches JWT
    print(auth.expires_at)      # when the JWT expires
"""

from dotenv import load_dotenv

from auth_client.provider import AuthProvider, AuthError
from auth_client.httpx_auth import InnateBearerAuth

# Last-resort fallback: load INNATE_SERVICE_KEY (and friends) from the
# system-wide env file written by post_update.sh. override=False so any
# value already set in the environment or by a per-process .env wins.
# load_dotenv is a no-op if the file is missing.
load_dotenv("/etc/innate/.env", override=False)

__all__: list[str] = ["AuthProvider", "AuthError", "InnateBearerAuth"]
