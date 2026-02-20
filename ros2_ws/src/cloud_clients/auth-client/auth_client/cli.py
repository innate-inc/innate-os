"""CLI: print a JWT to stdout."""

from __future__ import annotations

import os
import sys

from auth_client.provider import AuthProvider, AuthError


def main() -> None:
    issuer_url = os.environ.get("INNATE_AUTH_URL", "https://auth-v1.innate.bot")
    service_key = os.environ.get("INNATE_SERVICE_KEY", "")

    if not service_key:
        print("error: set INNATE_SERVICE_KEY", file=sys.stderr)
        raise SystemExit(1)

    try:
        auth = AuthProvider(issuer_url=issuer_url, service_key=service_key)
        print("INNATE_JWT=" + auth.token)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
