#!/bin/bash
# Fix corrupted .zsh_history file
# This script removes null bytes and truly invalid content from zsh history

HISTFILE="${ZDOTDIR:-$HOME}/.zsh_history"
BACKUP_DIR="${HOME}/.zsh_history_backups"

# Check if history file exists and is non-empty
if [[ ! -f "$HISTFILE" ]] || [[ ! -s "$HISTFILE" ]]; then
    exit 0
fi

# Check for NULL BYTES using grep -P (perl regex) with \x00
# This is the actual corruption that breaks zsh
if grep -qaP '\x00' "$HISTFILE" 2>/dev/null; then
    echo "[fix-zsh-history] Null bytes detected, repairing..."
    
    # Create backup directory and backup file
    mkdir -p "$BACKUP_DIR"
    BACKUP_FILE="$BACKUP_DIR/zsh_history_corrupted_$(date +%Y%m%d_%H%M%S)"
    cp "$HISTFILE" "$BACKUP_FILE"
    
    # Remove null bytes only, preserve everything else
    TMPFILE=$(mktemp)
    tr -d '\000' < "$HISTFILE" > "$TMPFILE"
    
    if [[ -s "$TMPFILE" ]]; then
        mv "$TMPFILE" "$HISTFILE"
        echo "[fix-zsh-history] Repaired. Backup: $BACKUP_FILE"
    else
        rm -f "$TMPFILE"
        : > "$HISTFILE"
        echo "[fix-zsh-history] Reset history. Backup: $BACKUP_FILE"
    fi
fi
