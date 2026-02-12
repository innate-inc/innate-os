# Innate-OS SSH welcome banner + update status.
# Source this file from ~/.zshrc.

# Only run for interactive SSH sessions.
if [[ ! -o interactive ]] || [[ -z "$SSH_CONNECTION" && -z "$SSH_TTY" ]]; then
    return 0 2>/dev/null || exit 0
fi

# Avoid printing multiple times when subshells are opened in the same session.
if [[ -n "${INNATE_WELCOME_SHOWN:-}" ]]; then
    return 0 2>/dev/null || exit 0
fi
export INNATE_WELCOME_SHOWN=1

INNATE_OS_ROOT="${INNATE_OS_ROOT:-$HOME/innate-os}"
UPDATE_BRANCH="${INNATE_UPDATE_BRANCH:-main}"
UPDATE_CMD="${INNATE_OS_ROOT}/scripts/update/innate-update"
ANIMATE="${INNATE_WELCOME_ANIMATE:-1}"
ANIMATION_DELAY="${INNATE_WELCOME_DELAY:-0.04}"

if [[ ! -x "$UPDATE_CMD" ]] && command -v innate-update >/dev/null 2>&1; then
    UPDATE_CMD="$(command -v innate-update)"
fi

# Colors (safe for plain terminals too).
NC=$'\033[0m'
BOLD=$'\033[1m'
CYAN=$'\033[0;36m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'

typeset -a LOGO_LINES=(
    '  ___ _   _ _   _    _  _____ _____ '
    ' |_ _| \ | | \ | |  / \|_   _| ____|'
    '  | ||  \| |  \| | / _ \ | | |  _|  '
    '  | || |\  | |\  |/ ___ \| | | |___ '
    ' |___|_| \_|_| \_/_/   \_\_| |_____|'
    '                 O S                 '
)

CURRENT_VERSION="unknown"
LATEST_TAG=""
if [[ -d "${INNATE_OS_ROOT}/.git" ]]; then
    CURRENT_VERSION="$(git -C "$INNATE_OS_ROOT" describe --tags --exact-match HEAD 2>/dev/null)"
    if [[ -z "$CURRENT_VERSION" ]]; then
        CURRENT_VERSION="$(git -C "$INNATE_OS_ROOT" describe --tags --always --dirty 2>/dev/null)"
    fi
    LATEST_TAG="$(git -C "$INNATE_OS_ROOT" tag --sort=-v:refname --merged "origin/$UPDATE_BRANCH" 2>/dev/null | head -1)"
    if [[ -z "$LATEST_TAG" ]]; then
        LATEST_TAG="$(git -C "$INNATE_OS_ROOT" tag --sort=-v:refname 2>/dev/null | head -1)"
    fi
fi

STATUS_MODE="unknown"
if [[ -x "$UPDATE_CMD" ]] && [[ -d "${INNATE_OS_ROOT}/.git" ]]; then
    if command -v timeout >/dev/null 2>&1; then
        timeout 4 "$UPDATE_CMD" quick-check >/dev/null 2>&1
    else
        "$UPDATE_CMD" quick-check >/dev/null 2>&1
    fi
    case $? in
        0) STATUS_MODE="latest" ;;
        1) STATUS_MODE="update" ;;
        *) STATUS_MODE="unknown" ;;
    esac
fi

echo ""
for line in "${LOGO_LINES[@]}"; do
    printf "%b%s%b\n" "$CYAN" "$line" "$NC"
    if [[ "$ANIMATE" == "1" ]] && [[ -t 1 ]]; then
        sleep "$ANIMATION_DELAY"
    fi
done
echo ""

printf "%bCurrent:%b %s\n" "$BOLD" "$NC" "${CURRENT_VERSION:-unknown}"
if [[ -n "$LATEST_TAG" ]]; then
    printf "%bLatest:%b  %s\n" "$BOLD" "$NC" "$LATEST_TAG"
fi

case "$STATUS_MODE" in
    latest)
        printf "%b[OK]%b You are on the latest Innate OS release.\n" "$GREEN" "$NC"
        ;;
    update)
        printf "%b[UPDATE]%b A newer Innate OS release is available.\n" "$YELLOW" "$NC"
        echo "Run: innate-update check"
        echo "Then: innate-update apply"
        ;;
    *)
        printf "%b[INFO]%b Unable to verify update status right now.\n" "$YELLOW" "$NC"
        ;;
esac

echo ""
