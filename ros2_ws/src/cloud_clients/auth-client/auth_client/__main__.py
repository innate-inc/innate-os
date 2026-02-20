"""CLI entry point: ``python -m auth_client`` prints a JWT to stdout.

Usage::

    python -m auth_client            # print once
    python -m auth_client watch       # print and auto-renew at half-life
"""

from __future__ import annotations

import os
import sys
import time

from auth_client.provider import AuthProvider, AuthError

_MIN_SLEEP: float = 5.0
_FALLBACK_SLEEP: float = 60.0


def _fetch_and_print(auth: AuthProvider) -> None:
    auth.token_needs_renewal = True
    print("INNATE_JWT=" + auth.token)


def main() -> None:
    issuer_url = os.environ.get("INNATE_AUTH_URL", "https://auth-v1.innate.bot")
    service_key = os.environ.get("INNATE_SERVICE_KEY", "")
    watch = len(sys.argv) > 1 and sys.argv[1] == "watch"

    if not service_key:
        print("error: set INNATE_SERVICE_KEY", file=sys.stderr)
        raise SystemExit(1)

    try:
        auth = AuthProvider(issuer_url=issuer_url, service_key=service_key)
        _fetch_and_print(auth)

        if watch:
            while True:
                delay = auth.seconds_until_renewal()
                if delay is None or delay <= 0:
                    delay = _FALLBACK_SLEEP
                delay = max(delay, _MIN_SLEEP)
                print(
                    f"# renewing in {delay:.0f}s (expires {auth.expires_at})",
                    file=sys.stderr,
                )
                time.sleep(delay)
                _fetch_and_print(auth)
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
