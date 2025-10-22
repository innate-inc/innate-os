#!/bin/bash
# GitHub App Authentication Helper
# Generates a short-lived token for git operations

set -e

APP_ID="${GITHUB_APP_ID:-}"
INSTALLATION_ID="${GITHUB_INSTALLATION_ID:-}"
PRIVATE_KEY_PATH="${GITHUB_APP_PRIVATE_KEY:-$HOME/.ssh/github-app.pem}"

if [ -z "$APP_ID" ] || [ -z "$INSTALLATION_ID" ]; then
    echo "Error: Set GITHUB_APP_ID and GITHUB_INSTALLATION_ID environment variables" >&2
    exit 1
fi

if [ ! -f "$PRIVATE_KEY_PATH" ]; then
    echo "Error: Private key not found at $PRIVATE_KEY_PATH" >&2
    exit 1
fi

# Generate JWT
now=$(date +%s)
iat=$((now - 60))
exp=$((now + 600))

header='{"alg":"RS256","typ":"JWT"}'
payload="{\"iat\":$iat,\"exp\":$exp,\"iss\":\"$APP_ID\"}"

header_b64=$(echo -n "$header" | openssl base64 -e | tr -d '=' | tr '/+' '_-' | tr -d '\n')
payload_b64=$(echo -n "$payload" | openssl base64 -e | tr -d '=' | tr '/+' '_-' | tr -d '\n')

signature=$(echo -n "${header_b64}.${payload_b64}" | \
    openssl dgst -sha256 -sign "$PRIVATE_KEY_PATH" | \
    openssl base64 -e | tr -d '=' | tr '/+' '_-' | tr -d '\n')

jwt="${header_b64}.${payload_b64}.${signature}"

# Get installation token
token=$(curl -s -X POST \
    -H "Authorization: Bearer $jwt" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/app/installations/$INSTALLATION_ID/access_tokens" | \
    grep -o '"token": *"[^"]*"' | sed 's/"token": *"\([^"]*\)"/\1/')

if [ -z "$token" ]; then
    echo "Error: Failed to get installation token" >&2
    exit 1
fi

echo "$token"

