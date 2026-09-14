#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x ../.venv/bin/python ]]; then
  uv sync --project ..
fi
if [[ ! -d node_modules ]]; then
  npm ci
fi
npm run build
exec ../.venv/bin/python server.py "$@"
