#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
# Upload a teleop recording (rosbag + metadata) to Google Drive via rclone.
#
# One-time setup (needs a browser on your laptop for the OAuth step):
#   ~/.local/bin/rclone config
#     n) new remote -> name: gdrive -> storage: drive -> leave the rest default
#     "Use web browser to automatically authenticate?" -> n  (robot is headless)
#     run the printed `rclone authorize "drive"` on your laptop, paste the token back
#
# Usage:
#   upload_recording.sh <recording_name> [drive_folder_id]
#     recording_name   e.g. teleop_20260718_080804 (a folder in data/recordings)
#     drive_folder_id  optional Drive folder to upload INTO; without it the
#                      files land in gdrive:mars-recordings/<recording_name>/
set -euo pipefail

RCLONE="${RCLONE:-$HOME/.local/bin/rclone}"
REMOTE="${RCLONE_REMOTE:-gdrive}"
ROOT="${INNATE_OS_ROOT:-$HOME/innate-os}"

if [[ $# -lt 1 ]]; then
    echo "Usage: $(basename "$0") <recording_name> [drive_folder_id]" >&2
    exit 1
fi

SRC="$ROOT/data/recordings/$1"
[[ -d $SRC ]] || { echo "ERROR: no such recording: $SRC" >&2; exit 1; }
"$RCLONE" listremotes | grep -q "^$REMOTE:" || {
    echo "ERROR: rclone remote '$REMOTE:' not configured — run '$RCLONE config' first (see header)." >&2
    exit 1
}

if [[ $# -ge 2 ]]; then
    DEST="$REMOTE:"
    FLAGS=(--drive-root-folder-id="$2")
else
    DEST="$REMOTE:mars-recordings/$1"
    FLAGS=()
fi

echo "Uploading $SRC -> $DEST"
"$RCLONE" copy "$SRC" "$DEST" "${FLAGS[@]}" --progress

echo "Verifying..."
"$RCLONE" check "$SRC" "$DEST" "${FLAGS[@]}" --one-way

echo "Done — upload verified."
