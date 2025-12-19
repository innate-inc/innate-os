#!/bin/bash
# Fetch GitHub release info for a given tag
# Usage: get-release-info.sh [tag]
# If no tag provided, uses the latest tag
# Outputs JSON with name and body fields

set -e

REPO="innate-inc/maurice-prod"
TAG="${1:-}"

# Get latest tag if not specified
if [ -z "$TAG" ]; then
    TAG=$(cd /home/jetson1/innate-os && git tag --list --sort=-version:refname 2>/dev/null | head -1)
fi

if [ -z "$TAG" ]; then
    echo '{"name":"","body":""}'
    exit 0
fi

# Try to get GitHub token
TOKEN=""
if [ -f /home/jetson1/innate-os/scripts/update/github-app-auth.sh ]; then
    TOKEN=$(/home/jetson1/innate-os/scripts/update/github-app-auth.sh 2>/dev/null || true)
fi

if [ -z "$TOKEN" ]; then
    # No token available, return empty
    echo '{"name":"","body":""}'
    exit 0
fi

# Fetch release info from GitHub API
RESPONSE=$(curl -s -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$REPO/releases/tags/$TAG" 2>/dev/null || echo '{}')

# Extract name and body, output as JSON
# Use python for reliable JSON handling
python3 << EOF
import json
import sys

try:
    data = json.loads('''$RESPONSE''')
    result = {
        "name": data.get("name", ""),
        "body": data.get("body", "")
    }
    print(json.dumps(result))
except:
    print('{"name":"","body":""}')
EOF
