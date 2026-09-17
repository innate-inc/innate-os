#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -d "${VIRTUAL_MARS_ASSETS:-../assets}/void_split_v2" ]]; then
  echo "Simulator assets are missing. Run ./innate-sim assets from the repository root, then retry." >&2
  exit 1
fi
if [[ ! -x ../.venv/bin/python ]]; then
  uv sync --project ..
fi
if [[ ! -d node_modules ]]; then
  npm ci
fi
npm run build
exec ../.venv/bin/python server.py "$@"
