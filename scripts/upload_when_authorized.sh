#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
# Detached companion to upload_recording.sh: waits for the rclone 'gdrive:'
# remote to exist (i.e. the operator finished the one-time `rclone config`
# OAuth), then uploads the recording and verifies it. Designed to be launched
# with setsid/nohup so it survives SSH disconnects:
#
#   setsid nohup scripts/upload_when_authorized.sh <recording> [folder_id] \
#       >> data/recordings/<recording>.upload.log 2>&1 < /dev/null &
#
# Polls every 30s for up to 14 days. Progress goes to the log file; a final
# "UPLOAD COMPLETE"/"UPLOAD FAILED" line makes the outcome grep-able.
set -uo pipefail

RCLONE="${RCLONE:-$HOME/.local/bin/rclone}"
REMOTE="${RCLONE_REMOTE:-gdrive}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POLL_S=30
MAX_WAIT_S=$((14 * 24 * 3600))

if [[ $# -lt 1 ]]; then
    echo "Usage: $(basename "$0") <recording_name> [drive_folder_id]" >&2
    exit 1
fi

echo "[$(date -Is)] waiting for rclone remote '$REMOTE:' (poll ${POLL_S}s)..."
waited=0
until "$RCLONE" listremotes 2>/dev/null | grep -q "^$REMOTE:"; do
    sleep "$POLL_S"
    waited=$((waited + POLL_S))
    if ((waited >= MAX_WAIT_S)); then
        echo "[$(date -Is)] UPLOAD FAILED: gave up after 14 days waiting for '$REMOTE:'"
        exit 1
    fi
done

echo "[$(date -Is)] remote '$REMOTE:' found — starting upload of $1"
if "$SCRIPT_DIR/upload_recording.sh" "$@"; then
    echo "[$(date -Is)] UPLOAD COMPLETE: $1"
else
    echo "[$(date -Is)] UPLOAD FAILED: $1 (see above)"
    exit 1
fi
