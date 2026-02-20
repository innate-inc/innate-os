# auth-client

Python library for obtaining and renewing JWTs from innate-auth via OIDC. Zero external dependencies.

## Install

```bash
pip install -e clients/auth-client
```

## Library usage

```python
from auth_client import AuthProvider

auth = AuthProvider(
    issuer_url="https://auth-v1.innate.bot",
    service_key="sk_...",
)
print(auth.token)       # lazily discovers OIDC + fetches JWT
print(auth.expires_at)  # when the JWT expires
```

## CLI

```bash
export INNATE_AUTH_URL="https://auth-v1.innate.bot"
export INNATE_SERVICE_KEY="sk_..."

# If pip-installed:
innate-auth-token

# Without installing (from repo root):
PYTHONPATH=clients/auth-client python -m auth_client
export $(PYTHONPATH=clients/auth-client python -m auth_client)

# Watch mode: auto-renew at half-life, printing each new JWT:
PYTHONPATH=clients/auth-client python -m auth_client watch
```

Prints `INNATE_JWT=<token>` to stdout. In watch mode, re-prints on each renewal.
