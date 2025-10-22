# Innate-OS Update Notification for SSH Login
# Source this in your .zshrc to show update notifications

# Only run on interactive shells and SSH sessions
if [[ -o interactive ]] && [[ -n "$SSH_CONNECTION" || -n "$SSH_TTY" ]]; then
    # Check for updates (uses cache, fast)
    if /home/jetson1/innate-os/scripts/update/innate-update --quick-check 2>/dev/null; then
        # Up to date - show nothing or minimal message
        : # Do nothing
    else
        # Updates available
        echo ""
        echo "╔════════════════════════════════════════════════════════╗"
        echo "║  ⚠  INNATE-OS UPDATES AVAILABLE                        ║"
        echo "╠════════════════════════════════════════════════════════╣"
        echo "║  Run: innate-update check                              ║"
        echo "║  Then: innate-update apply                             ║"
        echo "╚════════════════════════════════════════════════════════╝"
        echo ""
    fi
fi

