# Legacy shim: update_check.zsh now delegates to the dedicated welcome script.
# Keep this file to avoid breaking existing ~/.zshrc setups that still source it.

INNATE_OS_ROOT="${INNATE_OS_ROOT:-$HOME/innate-os}"
WELCOME_SCRIPT="${INNATE_OS_ROOT}/scripts/ssh_welcome.zsh"

if [[ -f "$WELCOME_SCRIPT" ]]; then
    source "$WELCOME_SCRIPT"
fi
