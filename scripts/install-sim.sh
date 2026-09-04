#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
#
# One-line installer for the Innate simulator:
#
#     curl -fsSL https://link.innate.bot/sim | sh
#
# It lives here so it can be read before it is piped to a shell, and so it
# versions with the thing it installs. Run from inside a checkout you already
# have, it sets that one up rather than cloning another.
#
# Installs whatever is missing (Docker, uv, git, the Linux rendering
# libraries), clones innate-os, and hands over to `./innate-sim setup`, which
# asks for the agent key and downloads the runtime. Nothing here duplicates the
# launcher's prerequisite checks -- `setup` runs them, so version floors and
# their remedies are stated in exactly one place.
#
# Every statement lives in a function called from main() on the last line: a
# truncated download must not half-execute.

# step() runs the install_* helpers through "$@", which shellcheck cannot trace
# shellcheck disable=SC2329
set -eu

REPO_URL="${INNATE_REPO_URL:-https://github.com/innate-inc/innate-os.git}"
REF="${INNATE_SIM_REF:-sim-stable}"
# Where `git clone` would put it: in the directory you ran this from.
INNATE_DIR_EXPLICIT=$([ -n "${INNATE_DIR:-}" ] && echo 1 || echo 0)
INNATE_DIR="${INNATE_DIR:-$(pwd)/innate-os}"
case "$INNATE_DIR" in
    # Relative paths become absolute here rather than at a dozen call sites:
    # `-weird` reads as an option to git, cd and ls; `/home/you/-weird` cannot.
    /*) ;;
    *) INNATE_DIR="$(pwd)/$INNATE_DIR" ;;
esac
UV_INSTALL_URL="https://astral.sh/uv/install.sh"
DOCKER_INSTALL_URL="https://get.docker.com"
GL_PACKAGES="libegl1 libgl1 libopengl0 libosmesa6"
# Disk each step needs, in GB, measured on a full install rather than guessed:
#
#   OS image      6.73 GB unpacked (1.4 GB compressed over 28 layers)
#   asset image   0.33 GB stored, plus 0.44 GB extracted to sim/assets
#   viewer bundle ~0.00 GB
#   host uv env   0.24 GB (sim/.venv)
#   volumes       0.32 GB (ros2_ws build/install/log + ccache)
#   checkout      0.90 GB including .git
#
# The image dominates: at 4.7x its compressed size it is most of the runtime
# figure, while the colcon volumes are a rounding error.
DISK_GB_DOCKER=2
DISK_GB_UV=1
DISK_GB_RENDER=1
DISK_GB_CLONE=1
DISK_GB_RUNTIME=8
DISK_HEADROOM_GB=2
DOCKER_WAIT_S=180
LOG_FILE="${INNATE_INSTALL_LOG:-$HOME/.innate-install.log}"
VERBOSE="${INNATE_VERBOSE:-0}"
LOG_TAIL_ROWS=3
# Distinct from any exit status a real command produces.
NO_GROUP_ROUTE=97
# Survives sudo's env_reset and sg, which inheritance does not.
BANNER_SHOWN="INNATE_BANNER_SHOWN=1"

PLATFORM=""
TMPDIR_INSTALL=""
NEED_RELOGIN=0
INTERACTIVE=0
PLAN=""
PLAN_N=0
DISK_NEEDED_GB=$DISK_HEADROOM_GB
DISK_FREE_GB=""
DISK_TARGET=""
SUDO_PRIMED=0
STEP_ACTIVE=0
# Timed from the moment the last question is answered: what the reader cares
# about is how long they were not needed for, not how long they spent reading.
UNATTENDED_FROM=0
STEP_WIDTH=80
step_pid=""
BLOCKED_REASON=""
ADOPTED_CHECKOUT=0

# Package managers ask questions no unattended install can answer, and
# needrestart prints a service-restart audit nobody asked for.
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

if [ -t 1 ]; then
    BOLD=$(printf '\033[1m')
    DIM=$(printf '\033[2m')
    RED=$(printf '\033[31m')
    GREEN=$(printf '\033[32m')
    YELLOW=$(printf '\033[33m')
    CYAN=$(printf '\033[36m')
    NC=$(printf '\033[0m')
else
    BOLD="" DIM="" RED="" GREEN="" YELLOW="" CYAN="" NC=""
fi

# COLORTERM is the usual signal, but ssh forwards only TERM (it is not in the
# default SendEnv), so a session reached over ssh arrives without it. A TERM
# advertising direct color is the second witness; without either, the logo
# falls back to flat cyan and nothing else changes.
case "${COLORTERM:-}:${TERM:-}" in
    truecolor:* | 24bit:*) TRUECOLOR=1 ;;
    *:*direct* | *:*truecolor*) TRUECOLOR=1 ;;
    *) TRUECOLOR=0 ;;
esac

# One line per step, in a right-aligned label column: what happened, never how.
# The how goes to LOG_FILE, which every failure points at.
say() { printf '  %s%8s%s  %s\n' "$CYAN" "$1" "$NC" "$2"; }
note() { printf '  %s%8s  %s%s\n' "$DIM" "" "$1" "$NC"; }

# The label, then the message with every later line held at the same indent --
# an explanation that falls back to the margin stops looking like one message.
labelled() {
    printf '%s\n' "$3" | {
        first=1
        while IFS= read -r labelled_line; do
            if [ "$first" -eq 1 ]; then
                printf '  %s%8s%s  %s\n' "$2" "$1" "$NC" "$labelled_line"
                first=0
            elif [ -n "$labelled_line" ]; then
                printf '  %8s  %s\n' "" "$labelled_line"
            else
                printf '\n' # a blank line stays blank, not twelve spaces
            fi
        done
    }
}

warn() { labelled "warning" "$YELLOW" "$*" >&2; }
cancel() {
    labelled "aborted" "$YELLOW" "$*"
    exit 0
}

die() {
    printf '\r\033[K'
    labelled "failed" "$RED" "$*" >&2
    if [ -s "$LOG_FILE" ]; then
        printf '  %s%8s  full log: %s%s\n' "$DIM" "" "$LOG_FILE" "$NC" >&2
    fi
    exit 1
}

spinner_frame() {
    n=$1
    set -- '⠋' '⠙' '⠹' '⠸' '⠼' '⠴' '⠦' '⠧' '⠇' '⠏'
    shift "$((n % 10))"
    printf '%s' "$1"
}

# step <label> <doing> <done> <command...>: run it with its output in the log,
# showing one live line that settles into a check mark. Two messages, because
# a spinner beside a noun does not say whether the noun is being installed or
# merely inspected.
step() {
    step_label=$1
    step_msg=$2
    step_done=$3
    shift 3
    if [ -z "$NC" ] || [ "$VERBOSE" != "0" ]; then
        say "$step_label" "$step_msg"
        if [ "$VERBOSE" != "0" ]; then
            # Verbose means the output is on screen AND in the log; tee loses
            # the command's status, so it is carried out through a file.
            { "$@" 2>&1; printf '%s' "$?" >"$TMPDIR_INSTALL/step-status"; } | tee -a "$LOG_FILE"
            return "$(cat "$TMPDIR_INSTALL/step-status")"
        fi
        "$@" >>"$LOG_FILE" 2>&1 || return 1
        return 0
    fi

    "$@" >>"$LOG_FILE" 2>&1 &
    step_pid=$!
    STEP_ACTIVE=1
    STEP_WIDTH=$(terminal_width)
    hide_cursor
    step_n=0
    while kill -0 "$step_pid" 2>/dev/null; do
        # Roughly once a second: a terminal resized during a ten-minute step
        # leaves every later frame built for a width that no longer exists,
        # and a frame wider than the terminal wraps -- which is one physical
        # row more than the redraw moves back up, so the frame walks down the
        # screen leaving a copy behind on every pass.
        [ "$((step_n % 8))" -eq 0 ] && STEP_WIDTH=$(terminal_width)
        # Elapsed from the frame count: this loop spawns enough processes per
        # second already without a `date` in it.
        draw_step_frame "$(spinner_frame "$step_n")" "$((step_n * 12 / 100))"
        step_n=$((step_n + 1))
        sleep 0.12
    done
    erase_step_frame
    show_cursor
    STEP_ACTIVE=0
    if ! wait "$step_pid"; then
        return 1
    fi
    printf '  %s%8s%s  %s✔%s %s\n' "$CYAN" "$step_label" "$NC" "$GREEN" "$NC" "$step_done"
}

# The status line plus the tail of the log, so a long step shows its work
# rather than a spinner that cannot be told apart from a hang. Always the same
# number of rows, so the cursor can be walked back over them.
draw_step_frame() {
    frame_width=$STEP_WIDTH
    # "  " + 8 label + "  " + spinner + " " = 14 before the message, then
    # "  " + elapsed + "s" after it; the log rows spend 14 on "  " + 8 + "  | ".
    frame_room=$((frame_width - 18 - ${#2}))
    tail_room=$((frame_width - 15))
    tail -n "$LOG_TAIL_ROWS" "$LOG_FILE" 2>/dev/null |
        tr -d '\r' | tr '\t' ' ' | sed "s/$(printf '\033')\[[0-9;]*[A-Za-z]//g" |
        cut -c "1-$tail_room" >"$TMPDIR_INSTALL/tail"

    # Built whole, written once. Clearing and drawing in separate writes leaves
    # the region blank for as long as the terminal takes to paint the next one,
    # which on Windows Terminal (WSL) is long enough to read as flicker; in one
    # write there is no intermediate state to see.
    frame=$(
        printf '\r\033[J'
        printf '\033[K  %s%8s%s  %s %s  %s%ss%s\n' \
            "$CYAN" "$step_label" "$NC" "$1" "$(printf '%s' "$step_msg" | cut -c "1-$frame_room")" \
            "$DIM" "$2" "$NC"
        tail_row=0
        while IFS= read -r tail_line; do
            printf '\033[K  %s%8s  │ %s%s\n' "$DIM" "" "$tail_line" "$NC"
            tail_row=$((tail_row + 1))
        done <"$TMPDIR_INSTALL/tail"
        while [ "$tail_row" -lt "$LOG_TAIL_ROWS" ]; do
            printf '\033[K\n'
            tail_row=$((tail_row + 1))
        done
        printf 'X' # sentinel: $( ) strips trailing newlines, and the last one counts
    )
    printf '%s\033[%dA\r' "${frame%X}" "$((LOG_TAIL_ROWS + 1))"
}

erase_step_frame() {
    erase_row=0
    while [ "$erase_row" -le "$LOG_TAIL_ROWS" ]; do
        printf '\033[K\n'
        erase_row=$((erase_row + 1))
    done
    printf '\033[%dA\r' "$((LOG_TAIL_ROWS + 1))"
}

# From the terminal itself (fd 3), not tput: with stdout on a pipe or inside a
# command substitution, tput reports terminfo's static 80 rather than the real
# width, and a status line wider than the terminal wraps -- which desynchronises
# every cursor-up the frame relies on.
terminal_width() {
    width=""
    if [ "$INTERACTIVE" -eq 1 ]; then
        width=$(stty size <&3 2>/dev/null | awk '{print $2}')
    fi
    if [ -z "$width" ]; then
        width=$(stty size </dev/tty 2>/dev/null | awk '{print $2}')
    fi
    if [ -z "$width" ]; then
        width=${COLUMNS:-}
    fi
    # `tput cols` last, and never trusted alone: it reads the size through
    # stdout, which is a pipe inside this command substitution, so it answers
    # with terminfo's static 80 whatever the terminal really is. Believing it
    # on a narrower one is what makes every frame wrap and stack.
    if [ -z "$width" ]; then
        width=$(tput cols 2>/dev/null || printf '80')
    fi
    case "$width" in
        '' | *[!0-9]*) width=80 ;;
    esac
    if [ "$width" -lt 40 ]; then
        width=40
    fi
    printf '%s' "$width"
}

# Privileged steps run in the background, where a password prompt would hang
# invisibly. Ask for it here, in the foreground, once.
prime_sudo_if_needed() {
    [ "$PLATFORM" = "macos" ] && return 0
    if ! have docker || { have apt-get && ! have_render_libs; }; then
        prime_sudo
    fi
}

have_render_libs() {
    # A cheap proxy for "apt would have work to do".
    [ -e /usr/lib/x86_64-linux-gnu/libEGL.so.1 ] || [ -e /usr/lib/aarch64-linux-gnu/libEGL.so.1 ]
}

prime_sudo() {
    [ "$SUDO_PRIMED" -eq 1 ] && return 0
    SUDO_PRIMED=1
    [ "$(id -u)" -eq 0 ] && return 0
    have sudo || die "This install needs root and sudo is not installed. Run it as root, or install sudo."
    sudo -n true 2>/dev/null && return 0
    note "administrator password needed to install system packages"
    with_tty sudo -v || die "Could not authenticate with sudo."
}

have() { command -v "$1" >/dev/null 2>&1; }

# Stop short without failing: the checkout and the other prerequisites are
# still worth having, and the reason is printed at the end where it is read.
blocked() { BLOCKED_REASON=$1; }

is_git_checkout() { git -C "$1" rev-parse --git-dir >/dev/null 2>&1; }

# A path is user input: it can hold spaces, and it can hold an apostrophe --
# which would close the quoting of any command we build around it. Both of
# these produce something a shell reads back as one word.
shell_quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# The same, but left bare when nothing in it needs quoting, so the common
# case stays copy-pasteable rather than defensively quoted.
display_path() {
    case "$1" in
        *[!A-Za-z0-9._/-]*) shell_quote "$1" ;;
        *) printf '%s' "$1" ;;
    esac
}

# A blinking cursor parked after the last thing drawn reads as an unanswered
# text prompt. cleanup() restores it, so no exit path can leave it hidden.
hide_cursor() {
    if [ -n "$NC" ]; then printf '\033[?25l'; fi
}

show_cursor() {
    if [ -n "$NC" ]; then printf '\033[?25h'; fi
}

# Invoked by the traps in main, which shellcheck does not trace
# shellcheck disable=SC2329
cleanup() {
    show_cursor
    [ -n "$TMPDIR_INSTALL" ] && rm -rf "$TMPDIR_INSTALL"
}

# A trap that only cleans up is not an interrupt handler: the shell resumes
# where it left off, so Ctrl+C would delete the temp dir and carry on
# installing. Stop the step's child, put the cursor back, and leave.
on_interrupt() {
    trap '' INT TERM
    if [ "$STEP_ACTIVE" -eq 1 ]; then
        kill "$step_pid" 2>/dev/null || true
        erase_step_frame
        STEP_ACTIVE=0
    fi
    printf '\n  %s%8s%s  interrupted; nothing else was installed\n' "$YELLOW" "stopped" "$NC" >&2
    exit 130
}

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    else
        have sudo || die "This step needs root and sudo is not installed. Run the installer as root, or install sudo."
        sudo "$@"
    fi
}

# The terminal on fd 3, NOT on stdin: piped into sh, stdin is the script
# itself, which the shell is still reading. Redirecting it makes the shell read
# the rest of the install from the keyboard -- it hangs at the end instead of
# exiting. Without a terminal at all (CI, a hook), the installer proceeds with
# defaults and setup skips the key prompt on its own.
attach_terminal() {
    # Redirections apply left to right, so `exec 3<... 2>/dev/null` still
    # reports its own failure; the group suppresses it.
    if [ -e /dev/tty ] && { exec 3</dev/tty; } 2>/dev/null; then
        INTERACTIVE=1
    else
        INTERACTIVE=0
    fi
}

# Anything that asks the user something needs the terminal as ITS stdin.
with_tty() {
    if [ "$INTERACTIVE" -eq 1 ]; then
        "$@" <&3
    else
        "$@"
    fi
}

# Control characters as values, since a case pattern cannot hold an escape.
ESC=$(printf '\033')
BACKSPACE=$(printf '\177')
DELETE=$(printf '\010')
CR=$(printf '\rX')
CR=${CR%X}
ETX=$(printf '\003')

# One keypress, named. dd because POSIX read has no single-character mode; the
# X sentinel survives command substitution stripping a trailing newline.
read_key() {
    key=$(dd bs=1 count=1 <&3 2>/dev/null; printf X)
    key=${key%X}
    case "$key" in
        "$ESC")
            key=$(dd bs=1 count=2 <&3 2>/dev/null; printf X)
            case "${key%X}" in
                '[A') key=up ;;
                '[B') key=down ;;
                '[C') key=right ;;
                '[D') key=left ;;
                *) key=escape ;;
            esac
            ;;
        "$CR" | '') key=enter ;;
        "$ETX") key=interrupt ;;
    esac
}

draw_confirm() {
    if [ "$1" -eq 1 ]; then
        printf '\r\033[K  %s%s%s   %s● Yes%s   %s○ No%s' \
            "$BOLD" "$confirm_question" "$NC" "$GREEN" "$NC" "$DIM" "$NC"
    else
        printf '\r\033[K  %s%s%s   %s○ Yes%s   %s● No%s' \
            "$BOLD" "$confirm_question" "$NC" "$DIM" "$NC" "$GREEN" "$NC"
    fi
}

# Left/right between Yes and No, like the menus in the launcher. Falls back to
# typing when the terminal cannot be put in raw mode.
# confirm <question> [no]: the second argument starts the selection on No,
# for questions whose yes costs something.
confirm() {
    [ "$INTERACTIVE" -eq 1 ] || return 0
    confirm_question=$1
    confirm_default=${2:-yes}
    if ! stty_saved=$(stty -g <&3 2>/dev/null); then
        printf '  %s%s [%s]: %s' "$YELLOW" "$confirm_question" \
            "$([ "$confirm_default" = "no" ] && printf 'y/N' || printf 'Y/n')" "$NC"
        read -r reply <&3 || reply=""
        case "$reply" in
            y | Y | yes | YES) return 0 ;;
            # Explicit returns: falling out of the case would carry on into
            # the raw-mode prompt below, which is the path that just failed.
            "")
                if [ "$confirm_default" = "no" ]; then return 1; fi
                return 0
                ;;
            *) return 1 ;;
        esac
    fi

    if [ "$confirm_default" = "no" ]; then confirm_yes=0; else confirm_yes=1; fi
    stty raw -echo <&3
    hide_cursor
    while :; do
        draw_confirm "$confirm_yes"
        read_key
        case "$key" in
            left | right | h | l) confirm_yes=$((1 - confirm_yes)) ;;
            y | Y) confirm_yes=1 ;;
            n | N) confirm_yes=0 ;;
            interrupt)
                stty "$stty_saved" <&3
                printf '\r\n'
                on_interrupt
                ;;
            enter) break ;;
        esac
    done
    stty "$stty_saved" <&3
    show_cursor
    draw_confirm "$confirm_yes"
    printf '\n'
    [ "$confirm_yes" -eq 1 ]
}

# plan_add <text> <GB it needs>: the estimate is a sum over the steps that
# will actually run, so someone who already has Docker is not quoted for it.
plan_add() {
    PLAN_N=$((PLAN_N + 1))
    DISK_NEEDED_GB=$((DISK_NEEDED_GB + $2))
    PLAN="$PLAN    $PLAN_N) $1
"
}

detect_platform() {
    case "$(uname -s)" in
        Darwin) PLATFORM="macos" ;;
        Linux)
            if [ -r /proc/version ] && grep -qi microsoft /proc/version; then
                PLATFORM="wsl"
            else
                PLATFORM="linux"
            fi
            ;;
        MINGW* | MSYS* | CYGWIN*)
            die "Git Bash cannot run the simulator. Install WSL2 (\`wsl --install -d Ubuntu\` in PowerShell), open the Ubuntu terminal, and rerun this command there."
            ;;
        *) die "Unsupported platform: $(uname -s). The simulator runs on macOS, Linux and WSL2." ;;
    esac
}

check_install_dir() {
    # A checkout under /mnt/c is on the Windows filesystem: every container
    # read crosses the 9p bridge (minutes, not seconds, per build) and the
    # ownership the launcher sets on workspace/ does not survive it.
    case "$PLATFORM:$INNATE_DIR" in
        wsl:/mnt/*)
            die "This folder is on the Windows drive, where the simulator runs far too
slowly to use. Your Linux home folder is the place for it.

Run these two commands:

  cd ~
  curl -fsSL https://link.innate.bot/sim | sh"
            ;;
    esac

    # Nesting a checkout inside another repo confuses both; the cwd default
    # makes that a real possibility, since a piped installer runs wherever the
    # terminal happened to be.
    if [ "$INNATE_DIR_EXPLICIT" -eq 0 ] && have git && enclosing=$(git rev-parse --show-toplevel 2>/dev/null); then
        # Cloning first and looking for the simulator afterwards is a normal
        # way to arrive. The clone is then the only step that does not apply --
        # Docker, uv, the rendering libraries and the version checks all still
        # do, and `innate-sim setup` performs none of them.
        if [ -x "$enclosing/innate-sim" ]; then
            INNATE_DIR=$enclosing
            ADOPTED_CHECKOUT=1
        else
            die "$(pwd) is inside a git repository, so the simulator would be a checkout within a checkout. cd somewhere else, or set INNATE_DIR to where it should live."
        fi
    fi
    if [ "$ADOPTED_CHECKOUT" -eq 0 ] &&
        [ -d "$INNATE_DIR" ] &&
        ! is_git_checkout "$INNATE_DIR" &&
        [ -n "$(ls -A "$INNATE_DIR" 2>/dev/null)" ]; then
        die "$INNATE_DIR already exists and is not an innate-os checkout. Move it aside, or set INNATE_DIR."
    fi

    parent=$(dirname "$INNATE_DIR")
    while [ ! -d "$parent" ] && [ "$parent" != "/" ]; do
        parent=$(dirname "$parent")
    done
    DISK_TARGET=$parent
    DISK_FREE_GB=$(df -Pk "$parent" | awk 'NR==2 {print int($4 / 1048576)}')
    case "$DISK_FREE_GB" in
        '' | *[!0-9]*) DISK_FREE_GB="" ;;
    esac
}

uv_installed() {
    have uv || [ -x "$HOME/.local/bin/uv" ] || [ -x "$HOME/.cargo/bin/uv" ]
}

# Listed in the order main() does them, which is the order the reader will
# watch happen. Keep the two in step: a plan that promises Docker before the
# clone tells someone their key is collected before the checkout exists.
# The list is what happens AFTER the questions, so the cloud-LLM choice is not
# in it: it is the next prompt, and a plan that previews the thing already on
# screen reads as a step out of order.
build_plan() {
    find_docker_cli || true
    have git || plan_add "Install git (with your package manager)" 1
    uv_installed || plan_add "Install uv, which runs the physics world (user-local, no sudo)" "$DISK_GB_UV"
    if [ "$ADOPTED_CHECKOUT" -eq 1 ]; then
        plan_add "Set up the innate-os checkout you are in ($INNATE_DIR)" 0
    elif is_git_checkout "$INNATE_DIR"; then
        plan_add "Update the innate-os checkout in $INNATE_DIR" 0
    else
        plan_add "Clone innate-os ($REF) into $INNATE_DIR" "$DISK_GB_CLONE"
    fi
    have docker || {
        if [ "$PLATFORM" = "macos" ]; then
            plan_add "Point you at Docker Desktop, which macOS needs you to install yourself" 0
        else
            plan_add "Install Docker Engine + Compose (from $DOCKER_INSTALL_URL, needs sudo)" "$DISK_GB_DOCKER"
        fi
    }
    if [ "$PLATFORM" != "macos" ] && have apt-get; then
        plan_add "Install the rendering libraries: $GL_PACKAGES (needs sudo)" "$DISK_GB_RENDER"
    fi
    plan_add "Download the simulator, so the first start is a start" "$DISK_GB_RUNTIME"
}

# Stated where the decision is made, not warned about above it: running out of
# disk half way through a multi-gigabyte pull is the expensive way to find out.
report_disk() {
    if [ -z "$DISK_FREE_GB" ]; then
        printf '  %sAbout %s GB of disk is needed.%s\n\n' "$DIM" "$DISK_NEEDED_GB" "$NC"
    elif [ "$DISK_FREE_GB" -lt "$DISK_NEEDED_GB" ]; then
        printf '  %sNeeds about %s GB of disk, but %s has only %s GB free.%s\n\n' \
            "$YELLOW" "$DISK_NEEDED_GB" "$DISK_TARGET" "$DISK_FREE_GB" "$NC"
    else
        printf '  %sDisk: about %s GB needed, %s GB free on %s.%s\n\n' \
            "$DIM" "$DISK_NEEDED_GB" "$DISK_FREE_GB" "$DISK_TARGET" "$NC"
    fi
}

review_plan() {
    printf '\n  %sThe installer will:%s\n\n' "$BOLD" "$NC"
    printf '%s\n' "$PLAN"
    report_disk
    if [ -z "$NC" ] || [ "$VERBOSE" != "0" ]; then
        printf '  %sCommand output is shown as it runs.%s\n\n' "$DIM" "$NC"
    else
        printf '  %sDetailed output goes to %s%s\n\n' "$DIM" "$LOG_FILE" "$NC"
    fi
    note "one question first, then it runs on its own"
    printf '\n'
    confirm "  Continue?" || cancel "nothing was installed"
    printf '\n'
}

install_git() {
    if have apt-get; then
        as_root apt-get update -qq && as_root apt-get install -y git
    elif have dnf; then
        as_root dnf install -y git
    else
        return 1
    fi
}

ensure_git() {
    have git && return 0
    if [ "$PLATFORM" = "macos" ]; then
        # Triggers the Command Line Tools GUI installer, which this script
        # cannot wait on -- so send the user back rather than racing it.
        xcode-select --install >/dev/null 2>&1 || true
        die "git is missing. Finish the Command Line Tools install macOS just offered, then rerun this command."
    fi
    prime_sudo
    step "git" "Installing git" "git installed" install_git || die "Could not install git."
}

docker_answers() {
    if [ "$NEED_RELOGIN" -eq 1 ]; then
        with_docker_group "docker info >/dev/null 2>&1" >/dev/null 2>&1
    else
        docker info >/dev/null 2>&1
    fi
}

# Is there any way to reach the socket before the next login? If not, the
# daemon step can only fail on the socket, so it is left to report_relogin.
docker_group_route_available() {
    [ "$NEED_RELOGIN" -eq 0 ] && return 0
    have sg || have sudo
}

start_docker_daemon() {
    docker_answers && return 0
    if [ "$PLATFORM" = "macos" ]; then
        open -a Docker >/dev/null 2>&1 || true
    elif have systemctl; then
        as_root systemctl start docker >/dev/null 2>&1 || true
    else
        # WSL without systemd: sysvinit is what docker.io ships there.
        as_root service docker start >/dev/null 2>&1 || true
    fi

    waited=0
    while [ "$waited" -lt "$DOCKER_WAIT_S" ]; do
        docker_answers && return 0
        sleep 3
        waited=$((waited + 3))
    done
    return 1
}

install_docker_linux() {
    curl -fsSL "$DOCKER_INSTALL_URL" -o "$TMPDIR_INSTALL/get-docker.sh"
    as_root sh "$TMPDIR_INSTALL/get-docker.sh"
    if [ "$(id -u)" -ne 0 ]; then
        as_root usermod -aG docker "$(id -un)"
        : >"$TMPDIR_INSTALL/docker-group-granted"
    fi
}

# A new group only reaches processes started after it is granted, so this
# shell cannot use the Docker socket yet. Read from the group database rather
# than a variable: the install runs inside step()'s background subshell, whose
# assignments never reach this one. `id -nG <user>` queries the database,
# `id -nG` reports this process's own credentials -- the difference IS the
# pending grant.
docker_group_pending() {
    # Capability first, membership second. Docker Desktop's WSL integration
    # serves a socket this user can already reach, while the distro's group
    # database may still show a grant an older session never picked up --
    # sending someone to log out of a Docker that works for them.
    docker info >/dev/null 2>&1 && return 1
    [ "$(id -u)" -eq 0 ] && return 1
    id -nG "$(id -un)" 2>/dev/null | tr ' ' '\n' | grep -qx docker || return 1
    id -nG 2>/dev/null | tr ' ' '\n' | grep -qx docker && return 1
    return 0
}

# install_docker_linux runs inside step()'s subshell, so it leaves a file
# rather than setting a variable this shell would never see.
docker_group_granted_here() { [ -f "$TMPDIR_INSTALL/docker-group-granted" ]; }

# macOS has no headless Docker: containers are Linux processes, so they need
# the VM that Docker Desktop provides, and installing it means a licence to
# accept, a privileged helper to authorise and a first launch to sit through.
# Homebrew can fetch it, but not finish it -- so this points at the download
# and gets out of the way rather than automating the half that can be.
# Docker Desktop 4.x can install its CLI into ~/.docker/bin instead of
# /usr/local/bin, and teaches PATH about it by editing ~/.zprofile -- which a
# bash shell never reads. The binary is right there; only the search is wrong.
DOCKER_CLI_DIRS="$HOME/.docker/bin /usr/local/bin /Applications/Docker.app/Contents/Resources/bin"

find_docker_cli() {
    have docker && return 0
    for docker_dir in $DOCKER_CLI_DIRS; do
        if [ -x "$docker_dir/docker" ]; then
            PATH="$docker_dir:$PATH"
            export PATH
            note "found the docker command in $docker_dir"
            return 0
        fi
    done
    return 1
}

require_docker_desktop() {
    if [ -d "/Applications/Docker.app" ]; then
        blocked "Docker Desktop is installed, but its docker command is nowhere this
installer can find it -- not on PATH, and not in ~/.docker/bin, /usr/local/bin or
the app itself.
Open Docker Desktop, then look at Settings -> Advanced: it can install the
command-line tools either system-wide or for your user only. Once 'docker version'
answers in a terminal, run this installer again."
    else
        blocked "Docker Desktop is not installed, and macOS needs it: containers are Linux
processes, so they run inside the VM it provides.
Download it: https://docs.docker.com/desktop/install/mac-install/
Install it, open it once and accept its terms, then run this installer again."
    fi
    return 1
}

ensure_docker() {
    find_docker_cli || true
    if ! have docker; then
        if [ "$PLATFORM" = "macos" ]; then
            require_docker_desktop || return 0
        else
            prime_sudo
            step "docker" "Installing Docker Engine + Compose" "Docker Engine + Compose installed" install_docker_linux || die "Could not install Docker."
        fi
    fi

    [ -n "$BLOCKED_REASON" ] && return 0
    if docker_group_pending; then
        NEED_RELOGIN=1
        if docker_group_granted_here; then
            note "added you to the docker group"
        else
            note "this session predates your docker group membership"
        fi
    fi
    docker_group_route_available || return 0
    if [ "$PLATFORM" = "macos" ] && ! docker info >/dev/null 2>&1; then
        note "opening Docker Desktop -- accept its terms if it asks, and leave it running"
    fi
    if ! step "docker" "Starting the Docker daemon" "Docker daemon running" start_docker_daemon; then
        if [ "$PLATFORM" = "macos" ]; then
            die "Docker Desktop did not finish starting within ${DOCKER_WAIT_S}s.
Its window should be in front of you -- accept the terms if it is asking, wait for the
whale icon to settle, then run this installer again."
        fi
        die "The Docker daemon is installed but did not start. Start it (sudo systemctl start docker) and rerun this command."
    fi
}

install_uv() {
    curl -LsSf "$UV_INSTALL_URL" -o "$TMPDIR_INSTALL/uv-install.sh"
    sh "$TMPDIR_INSTALL/uv-install.sh"
}

ensure_uv() {
    uv_installed || step "uv" "Installing uv" "uv installed" install_uv || die "Could not install uv."
    # The installer's default location, which the current shell does not have
    # on PATH yet. Same paths the launcher looks in (runtime.find_uv).
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
}

install_render_libs() {
    as_root apt-get update -qq
    # GL_PACKAGES is a package list, so it has to word-split
    # shellcheck disable=SC2086
    as_root apt-get install -y $GL_PACKAGES
}

ensure_render_libs() {
    [ "$PLATFORM" = "macos" ] && return 0
    if ! have apt-get; then
        warn "Install your distro's OpenGL/OSMesa runtime if the 3D view fails to start (Fedora: mesa-libEGL mesa-libGL mesa-libOSMesa)."
        return 0
    fi
    prime_sudo
    step "render" "Installing the OpenGL libraries" "OpenGL libraries installed" install_render_libs || die "Could not install the rendering libraries."
}

update_checkout() {
    git -C "$INNATE_DIR" fetch --quiet origin "$REF" || return 1
    if [ -n "$(git -C "$INNATE_DIR" status --porcelain)" ]; then
        return 0 # local changes are the user's; never touch them
    fi
    git -C "$INNATE_DIR" merge --ff-only FETCH_HEAD >/dev/null 2>&1 || return 0
}

clone_checkout() {
    # Blobless rather than shallow: full history for log/bisect at a fraction
    # of the size, and `git pull` keeps working afterwards.
    git clone --quiet --filter=blob:none --branch "$REF" -- "$REPO_URL" "$INNATE_DIR"
}

clone_repo() {
    # Never touch a checkout the user brought: they chose that branch, and an
    # installer is not the place to move it.
    if [ "$ADOPTED_CHECKOUT" -eq 1 ]; then
        note "using $INNATE_DIR at $(git -C "$INNATE_DIR" rev-parse --short HEAD)"
        return 0
    fi
    if is_git_checkout "$INNATE_DIR"; then
        step "repo" "Updating $INNATE_DIR" "updated $INNATE_DIR" update_checkout || die "Could not update $INNATE_DIR."
    else
        git ls-remote --exit-code --heads "$REPO_URL" "$REF" >/dev/null 2>&1 ||
            die "$REPO_URL has no branch named $REF. Set INNATE_SIM_REF to one that exists (main, for the development tip), or report this at https://discord.gg/innate."
        step "repo" "Cloning innate-os into $INNATE_DIR" "cloned into $INNATE_DIR" clone_checkout || die "Could not clone $REPO_URL."
    fi
    note "$REF at $(git -C "$INNATE_DIR" rev-parse --short HEAD)"
}

# Run a command with the docker group applied, without a new login session.
# `sg` is the direct way; `sudo -u you` is the same effect by another route,
# because sudo re-reads the target user's groups from the database -- and
# minimized WSL images ship neither sg nor newgrp (both live in `passwd`).
# Returns NO_GROUP_ROUTE when neither exists -- not 127, which the inner
# shell also returns for a command it cannot find, and which would turn a
# failed setup into a cheerful "log out and back in".
with_docker_group() {
    if have sg; then
        with_tty sg docker -c "$1"
    elif have sudo; then
        with_tty sudo -u "$(id -un)" -- sh -c "$1"
    else
        return "$NO_GROUP_ROUTE"
    fi
}

# The same two routes, replacing this process rather than waiting on one.
exec_in_docker_group() {
    if have sg; then
        exec sg docker -c "$1"
    elif have sudo; then
        exec sudo -u "$(id -un)" -- sh -c "$1"
    fi
}

# Asked while the user is still at the keyboard, right after the clone, so
# everything slow that follows can run unattended. Needs the checkout (the
# wizard lives in the launcher) and uv (which supplies the Python it runs on).
# The cloud-LLM question, asked before anything is installed -- so the answer
# arrives in the first ten seconds rather than after apt, uv and a clone.
#
# Only the ANSWER is collected here. Which key goes into .env, and which get
# commented out, stays in the launcher's wizard (setup_wizard.apply_brain_backend)
# so that lives in one place. The keys reach it on stdin -- brain key, then the
# optional Cartesia one, a line each -- never a temp file, never an environment
# a process listing can show.
LLM_BACKEND=""
LLM_KEY=""
VOICE_KEY=""

draw_llm_options() {
    llm_row=0
    for llm_label in "Your own API keys     a Gemini key, plus Cartesia for the robot's voice" \
        "Innate service key    ships with a MARS robot, or ask on https://discord.gg/innate" \
        "None                  run the simulator without an agent"; do
        llm_row=$((llm_row + 1))
        if [ "$llm_row" -eq "$1" ]; then
            printf '\r\033[K  %s●%s %s%s%s\n' "$GREEN" "$NC" "$BOLD" "$llm_label" "$NC"
        else
            printf '\r\033[K  %s○ %s%s\n' "$DIM" "$llm_label" "$NC"
        fi
    done
}

ask_llm_backend() {
    if [ "$INTERACTIVE" -eq 0 ]; then
        LLM_BACKEND=""
        return 0
    fi
    printf '\n  %sHow should the robot'"'"'s agent reach a cloud LLM?%s\n' "$BOLD" "$NC"
    printf '  %sThe agent runs on the robot either way; this is only which keys it uses.%s\n\n' "$DIM" "$NC"

    llm_choice=1
    if ! stty_saved=$(stty -g <&3 2>/dev/null); then
        # No raw mode: the launcher asks its own way, after the clone.
        LLM_BACKEND=""
        return 0
    fi
    stty raw -echo <&3
    hide_cursor
    llm_redraw=0
    while :; do
        [ "$llm_redraw" -eq 1 ] && printf '\033[3A'
        draw_llm_options "$llm_choice"
        llm_redraw=1
        read_key
        case "$key" in
            up | k) llm_choice=$(((llm_choice + 1) % 3 + 1)) ;;
            down | j) llm_choice=$((llm_choice % 3 + 1)) ;;
            1 | 2 | 3) llm_choice=$key ;;
            interrupt)
                stty "$stty_saved" <&3
                show_cursor
                printf '\r\n'
                on_interrupt
                ;;
            enter) break ;;
        esac
    done
    stty "$stty_saved" <&3
    show_cursor

    case "$llm_choice" in
        1) LLM_BACKEND=gemini ;;
        2) LLM_BACKEND=innate ;;
        *) LLM_BACKEND=none ;;
    esac
    [ "$LLM_BACKEND" = "none" ] && return 0
    read_llm_key
}

# The brain key, then -- for your own keys -- the optional one that gives the
# robot a voice. Both travel to the launcher on one pipe, a line each.
read_llm_key() {
    if [ "$LLM_BACKEND" = "gemini" ]; then
        read_secret "Paste your Gemini API key"
        LLM_KEY=$SECRET_VALUE
        printf '  %sOptional: a Cartesia key lets the robot speak (https://play.cartesia.ai/keys).%s\n' "$DIM" "$NC"
        read_secret "Paste your Cartesia API key, or Enter to skip" optional
        VOICE_KEY=$SECRET_VALUE
    else
        read_secret "Paste your Innate service key"
        LLM_KEY=$SECRET_VALUE
    fi
}

# Masked, with a length counter: a paste that silently doubled is the failure
# worth showing, and the key itself never belongs on screen. Answers in
# SECRET_VALUE; a second argument lets Enter accept an empty one.
read_secret() {
    secret_prompt=$1
    secret_optional=${2:-}
    stty_saved=$(stty -g <&3)
    stty raw -echo <&3
    hide_cursor
    SECRET_VALUE=""
    while :; do
        printf '\r\033[K  %s%s: %s%s' "$YELLOW" "$secret_prompt" "$NC" "$(printf '%*s' "${#SECRET_VALUE}" '' | tr ' ' '*')"
        [ -n "$SECRET_VALUE" ] && printf ' %s(%s)%s' "$DIM" "${#SECRET_VALUE}" "$NC"
        read_key
        case "$key" in
            enter) { [ -n "$SECRET_VALUE" ] || [ -n "$secret_optional" ]; } && break ;;
            interrupt)
                stty "$stty_saved" <&3
                show_cursor
                printf '\r\n'
                on_interrupt
                ;;
            escape | up | down | left | right) ;;
            "$BACKSPACE" | "$DELETE") SECRET_VALUE=${SECRET_VALUE%?} ;;
            *) SECRET_VALUE="$SECRET_VALUE$key" ;;
        esac
    done
    stty "$stty_saved" <&3
    show_cursor
    printf '\r\033[K  %s%s: %s%s %s(%s)%s\n' "$YELLOW" "$secret_prompt" "$NC" \
        "$(printf '%*s' "${#SECRET_VALUE}" '' | tr ' ' '*')" "$DIM" "${#SECRET_VALUE}" "$NC"
}

ask_setup_questions() {
    printf '\n'
    if [ -n "$LLM_BACKEND" ]; then
        # Already answered, before anything was installed. The launcher writes
        # .env; the key travels down a pipe between our two processes.
        if printf '%s\n%s' "$LLM_KEY" "$VOICE_KEY" |
            sh -c "cd -- $(shell_quote "$INNATE_DIR") && $BANNER_SHOWN ./innate-sim setup --no-prefetch --backend $LLM_BACKEND"; then
            LLM_KEY=""
            VOICE_KEY=""
            return 0
        fi
    elif with_docker_group_or_plain "cd -- $(shell_quote "$INNATE_DIR") && $BANNER_SHOWN ./innate-sim setup --no-prefetch"; then
        return 0
    fi
    die "Could not save your key (see above). Fix the problem, then rerun:
  cd -- $(display_path "$INNATE_DIR") && ./innate-sim setup"
}

# The long half: images, geometry, the world's Python environment. No prompts.
run_prefetch() {
    printf '\n'
    if with_docker_group_or_plain "cd -- $(shell_quote "$INNATE_DIR") && $BANNER_SHOWN ./innate-sim setup --prefetch-only"; then
        return 0
    fi
    die "The download did not finish (see above). Fix the problem, then rerun:
  cd -- $(display_path "$INNATE_DIR") && ./innate-sim setup"
}

# Under the docker group when this shell predates it, plainly when it does not.
with_docker_group_or_plain() {
    if [ "$NEED_RELOGIN" -eq 0 ]; then
        with_tty sh -c "$1"
        return $?
    fi
    if with_docker_group "$1"; then
        return 0
    elif [ $? -ne "$NO_GROUP_ROUTE" ]; then
        return 1
    fi
    report_relogin
    exit 0
}

report_blocked() {
    printf '\n'
    labelled "blocked" "$YELLOW" "$BLOCKED_REASON"
    printf '\n'
    # The installer, not `innate-sim setup`: a Docker installed by hand brings
    # its own Compose, which may be a 5.x that cannot mount the sim's assets --
    # setup would only refuse it, while this fixes it and carries on.
    printf '  %s%8s%s  when that is done, run this again:\n\n' "$BOLD" "next" "$NC"
    printf '  %8s  cd %s\n' "" "$(display_path "$INNATE_DIR")"
    printf '  %8s  sh scripts/install-sim.sh\n\n' ""
    note "everything else is installed and the checkout is ready, so it will be quick"
    printf '\n'
}

# The launcher's wordmark (dashboard.ASCII_BANNER) and its green-to-gold
# gradient, so the install ends in the same skin the dashboard opens in.
logo_color() {
    [ -n "$NC" ] || return 0
    if [ "$TRUECOLOR" -eq 0 ]; then
        printf '\033[36m'
        return 0
    fi
    case "$1" in
        1) printf '\033[38;2;119;202;155m' ;;
        2) printf '\033[38;2;140;199;140m' ;;
        3) printf '\033[38;2;164;196;126m' ;;
        4) printf '\033[38;2;185;194;115m' ;;
        *) printf '\033[38;2;203;192;108m' ;;
    esac
}

print_logo() {
    i=0
    while IFS= read -r line; do
        i=$((i + 1))
        printf '%s%s%s%s\n' "$(logo_color "$i")" "$BOLD" "$line" "$NC"
    done <<'EOF'
 ___ _   _ _   _    _  _____ _____
|_ _| \ | | \ | |  / \|_   _| ____|
 | ||  \| |  \| | / _ \ | | |  _|
 | || |\  | |\  |/ ___ \| | | |___
|___|_| \_|_| \_/_/   \_\_| |_____|
EOF
}

print_intro() {
    printf '\n'
    print_logo
    printf '  %ssimulator installer%s\n\n' "$DIM" "$NC"
}

detect_editor() {
    for candidate in cursor code windsurf zed subl; do
        if have "$candidate"; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

report_blocked() {
    printf '\n'
    labelled "blocked" "$YELLOW" "$BLOCKED_REASON"
    printf '\n'
    # The installer, not `innate-sim setup`: a Docker installed by hand brings
    # its own Compose, which may be a 5.x that cannot mount the sim's assets --
    # setup would only refuse it, while this fixes it and carries on.
    printf '  %s%8s%s  when that is done, run this again:\n\n' "$BOLD" "next" "$NC"
    printf '  %8s  cd %s\n' "" "$(display_path "$INNATE_DIR")"
    printf '  %8s  sh scripts/install-sim.sh\n\n' ""
    note "everything else is installed and the checkout is ready, so it will be quick"
    printf '\n'
}

report_relogin() {
    printf '\n%sAlmost there.%s Your user was just added to the docker group, which only\n' "$BOLD" "$NC"
    printf 'takes effect in a new login session, so log out and back in. (newgrp docker\n'
    printf 'does it without one, on the distros that ship the passwd package.)\n'
    printf 'Then finish the install:\n\n'
    printf '  cd %s && ./innate-sim setup\n\n' "$(display_path "$INNATE_DIR")"
}

# The launcher's wordmark (dashboard.ASCII_BANNER) and its green-to-gold
# gradient, so the install ends in the same skin the dashboard opens in.
logo_color() {
    [ -n "$NC" ] || return 0
    if [ "$TRUECOLOR" -eq 0 ]; then
        printf '\033[36m'
        return 0
    fi
    case "$1" in
        1) printf '\033[38;2;119;202;155m' ;;
        2) printf '\033[38;2;140;199;140m' ;;
        3) printf '\033[38;2;164;196;126m' ;;
        4) printf '\033[38;2;185;194;115m' ;;
        *) printf '\033[38;2;203;192;108m' ;;
    esac
}

print_logo() {
    i=0
    while IFS= read -r line; do
        i=$((i + 1))
        printf '%s%s%s%s\n' "$(logo_color "$i")" "$BOLD" "$line" "$NC"
    done <<'EOF'
 ___ _   _ _   _    _  _____ _____
|_ _| \ | | \ | |  / \|_   _| ____|
 | ||  \| |  \| | / _ \ | | |  _|
 | || |\  | |\  |/ ___ \| | | |___
|___|_| \_|_| \_/_/   \_\_| |_____|
EOF
}

print_intro() {
    printf '\n'
    print_logo
    printf '  %ssimulator installer%s\n\n' "$DIM" "$NC"
}

detect_editor() {
    for candidate in cursor code windsurf zed subl; do
        if have "$candidate"; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

report_blocked() {
    printf '\n'
    labelled "blocked" "$YELLOW" "$BLOCKED_REASON"
    printf '\n'
    # The installer, not `innate-sim setup`: a Docker installed by hand brings
    # its own Compose, which may be a 5.x that cannot mount the sim's assets --
    # setup would only refuse it, while this fixes it and carries on.
    printf '  %s%8s%s  when that is done, run this again:\n\n' "$BOLD" "next" "$NC"
    printf '  %8s  cd %s\n' "" "$(display_path "$INNATE_DIR")"
    printf '  %8s  sh scripts/install-sim.sh\n\n' ""
    note "everything else is installed and the checkout is ready, so it will be quick"
    printf '\n'
}

format_duration() {
    if [ "$1" -ge 3600 ]; then
        printf '%dh%02dm' "$(($1 / 3600))" "$((($1 % 3600) / 60))"
    elif [ "$1" -ge 60 ]; then
        printf '%dm%02ds' "$(($1 / 60))" "$(($1 % 60))"
    else
        printf '%ds' "$1"
    fi
}

report_next_steps() {
    printf '\n'
    say "ready" "the simulator is installed"
    if [ "$UNATTENDED_FROM" -gt 0 ]; then
        note "took $(format_duration "$(($(date +%s 2>/dev/null || echo "$UNATTENDED_FROM") - UNATTENDED_FROM))") after your last answer"
    fi
    printf '\n'
    # No script can cd its parent's shell, so the cd is the user's own first
    # step -- and everything below is written to follow it.
    printf '  %s%8s%s  cd %s\n' "$BOLD" "start" "$NC" "$(display_path "$INNATE_DIR")"
    printf '  %8s  ./innate-sim up\n\n' ""
    if editor=$(detect_editor); then
        printf '  %s%8s%s  %s .  %s— skills and agents live in workspace/%s\n' "$BOLD" "edit" "$NC" "$editor" "$DIM" "$NC"
    else
        printf '  %s%8s%s  open it in your editor  %s— skills and agents live in workspace/%s\n' \
            "$BOLD" "edit" "$NC" "$DIM" "$NC"
    fi
    printf '  %s%8s%s  https://localhost  %s— once it is up (accept the self-signed certificate)%s\n' \
        "$BOLD" "open" "$NC" "$DIM" "$NC"
    printf '  %s%8s%s  https://discord.gg/innate\n' "$BOLD" "help" "$NC"
    printf '\n'
    if [ "$NEED_RELOGIN" -eq 1 ]; then
        note "docker was installed just now, so this shell is not in its group yet;"
        note "innate-sim reruns itself under sg until your next login session."
        printf '\n'
    fi
}

# Offer to skip the last command too. `exec` hands the terminal to the
# launcher: its dashboard owns the screen from here, and the shell is gone
# rather than waiting behind it.
offer_to_start() {
    [ "$INTERACTIVE" -eq 1 ] || return 0
    confirm "  Start the simulator now?" || return 0
    printf '\n'
    exec 0<&3
    if [ "$NEED_RELOGIN" -eq 1 ]; then
        # Same reason as run_setup: this process has no docker group, so
        # exec-ing the launcher directly would hand it a socket it cannot open.
        exec_in_docker_group "$BANNER_SHOWN $(shell_quote "$INNATE_DIR/innate-sim") up"
        report_relogin
        exit 0
    fi
    exec env INNATE_BANNER_SHOWN=1 "$INNATE_DIR/innate-sim" up
}

main() {
    TMPDIR_INSTALL=$(mktemp -d)
    trap cleanup EXIT
    trap on_interrupt INT TERM

    : >"$LOG_FILE" 2>/dev/null || LOG_FILE="$TMPDIR_INSTALL/install.log"
    attach_terminal
    print_intro
    # The wordmark is this screen's. Exporting is not enough on its own:
    # sudo resets the environment by default and sg is setuid, so the
    # commands below carry the assignment in the command line too.
    export INNATE_BANNER_SHOWN=1
    detect_platform
    check_install_dir
    build_plan
    review_plan

    # Questions first: git, uv and the clone are quick and silent, and the
    # wizard needs all three. Everything that can prompt -- sudo included --
    # is asked before the downloads rather than in the middle of them.
    ask_llm_backend
    UNATTENDED_FROM=$(date +%s 2>/dev/null || echo 0)
    prime_sudo_if_needed
    ensure_git
    ensure_uv
    clone_repo
    ask_setup_questions

    ensure_docker
    ensure_render_libs

    if [ -n "$BLOCKED_REASON" ]; then
        # Everything else is in place; what is left needs their Docker, so
        # stop before a download that Docker cannot use.
        report_blocked
        exit 0
    fi
    run_prefetch
    report_next_steps
    offer_to_start
}

main "$@"
# Explicit, so the shell stops here rather than reading whatever else is on
# stdin -- the pipe this script arrived through.
exit $?
