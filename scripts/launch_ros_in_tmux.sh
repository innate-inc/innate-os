#!/bin/zsh
# Launch ROS nodes in tmux windows, one pane per |-delimited command

SESSION_NAME="ros_nodes"
ROS_WS_PATH="$INNATE_OS_ROOT/ros2_ws"
DDS_SETUP_SCRIPT="$INNATE_OS_ROOT/config/dds/setup_dds.zsh"

# ros-app.service runs as root and drops here through sudo, so this is the first point
# that knows the uid the audio session actually belongs to. Every pane inherits it.
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

RUNTIME_ENV_EXPORTS=$(python3 "$INNATE_OS_ROOT/scripts/print_runtime_env.py" --shell 2>/dev/null || true)
WEBAPP_URI_SCRIPT="$INNATE_OS_ROOT/scripts/webapp_uri.zsh"
if [ -f "$WEBAPP_URI_SCRIPT" ]; then
    source "$WEBAPP_URI_SCRIPT"
fi

# Detect a missing service key up front so we can warn loudly once launch is done.
# Uses the same /etc/innate.env + repo .env merge as the runtime env, so this
# matches what the nodes actually see (and treats an empty value as missing).
# Match on the explicit "missing" token rather than the exit code: a crash in the
# checker would also exit non-zero, and we must not show a false "no key" banner
# when a key is actually present. No token (crash/empty) => assume present.
SERVICE_KEY_MISSING=false
if [ "$(python3 "$INNATE_OS_ROOT/scripts/print_runtime_env.py" --has-service-key 2>/dev/null)" = "missing" ]; then
    SERVICE_KEY_MISSING=true
fi

# ROS launch commands grouped into windows (pipe-delimited; one pane per command)
ROS_COMMAND_GROUPS=(
    "ros2 launch mars_control app.launch.py|ros2 launch mars_bringup mars_bringup.launch.py"
    "ros2 launch mars_arm arm.launch.py|ros2 launch manipulation recorder.launch.py"
    "ros2 launch brain_client brain_client.launch.py|sleep 5 && ros2 service call /calibrate std_srvs/srv/Trigger && sleep 5 && ros2 launch mars_nav mode_manager.launch.py"
    "ros2 launch manipulation behavior.launch.py|ros2 launch brain_client input_manager.launch.py"
    "ros2 launch mars_cam camera_composable.launch.py|ros2 launch mars_control udp_leader_receiver.launch.py"
    "ros2 launch mars_arm ik.launch.py|cd ~/innate-os && ros2 launch innate_logger logger.launch.py"
    "cd ~/innate-os && ros2 run innate_training_node training_node|cd ~/innate-os && ros2 launch innate_uninavid uninavid.launch.py"
    "cd ~/innate-os && ros2 launch innate_console console.launch.py|cd ~/innate-os/webapp && while true; do INNATE_HTTP_PORT=4080 python3 proxy/https_server.py 4443; sleep 2; done"
    "ros2 launch dataset_encoder dataset_encoder.launch.py"
)

WINDOW_NAMES=(
    "app-bringup"
    "arm-recorder"
    "brain-nav"
    "behaviors-inputs"
    "cam-leader"
    "ik-logger"
    "training-uninavid"
    "console-webapp"
    "encoder"
)

# Collapse the per-pane environment setup (runtime env exports + DDS/ROS
# sourcing) into one file the panes source. This keeps the command echoed in
# each pane short and, importantly, keeps the service key off the screen and
# out of the scrollback.
#
# The file holds INNATE_SERVICE_KEY, so create it securely: mktemp makes it
# atomically at mode 0600 with an unpredictable name, closing the umask race
# (a plain `>` would briefly create it world-readable) and the symlink-attack
# window on a predictable /tmp path.
PANE_SETUP_FILE=$(mktemp "${TMPDIR:-/tmp}/innate_pane_env.XXXXXX") || {
    echo "ERROR: Failed to create pane setup tempfile." >&2
    exit 1
}
# Clean up the service-key-bearing tempfile on the error-exit paths below. The
# normal path can't rely on this trap (the script ends in `exec sleep infinity`,
# which never fires EXIT) — a backgrounded delete handles that case instead.
trap 'rm -f "$PANE_SETUP_FILE"' EXIT INT TERM
{
    echo "${RUNTIME_ENV_EXPORTS:-true}"
    echo "source $DDS_SETUP_SCRIPT"
    echo "source $ROS_WS_PATH/install/setup.zsh"
} > "$PANE_SETUP_FILE"
PANE_SETUP_CMD="source $PANE_SETUP_FILE"

echo "Launching ROS nodes in tmux session '$SESSION_NAME'..."

# Source environment
source "$DDS_SETUP_SCRIPT" || { echo "ERROR: Failed to source DDS setup." >&2; exit 1; }

if [ -f "$ROS_WS_PATH/install/setup.zsh" ]; then
    source "$ROS_WS_PATH/install/setup.zsh" || { echo "ERROR: Failed to source ROS workspace." >&2; exit 1; }
else
    echo "ERROR: ROS workspace setup not found at $ROS_WS_PATH/install/setup.zsh" >&2
    exit 1
fi

if tmux has-session -t $SESSION_NAME 2>/dev/null; then
    tmux kill-session -t $SESSION_NAME
    sleep 1
fi

process_command_group() {
    local group_index=$1
    local command_group="${ROS_COMMAND_GROUPS[$group_index]}"
    local window_name="${WINDOW_NAMES[$group_index]}"
    local commands=(${(s:|:)command_group})
    
    echo "  Creating window: $window_name"
    
    if [ $group_index -eq 1 ]; then
        tmux new-session -d -s $SESSION_NAME -n "$window_name" -c ~ || return 1
    else
        tmux new-window -t $SESSION_NAME -n "$window_name" -c ~ || return 1
    fi
    
    sleep 0.1

    # Address each pane by its unique pane-id (captured from split-window) rather
    # than the window's *active* pane. The active-pane approach relied on
    # split-window having just made the intended pane active plus a fixed sleep,
    # so a slow/loaded box, a tmux hook, or a layout side-effect could misroute
    # keys to the wrong pane. Pane-ids are exact, with no timing dependency.
    local first_pane
    first_pane=$(tmux display-message -p -t $SESSION_NAME:"$window_name" '#{pane_id}')
    tmux send-keys -t "$first_pane" "$PANE_SETUP_CMD && ${commands[1]}" C-m || return 1

    local idx pane_id
    for (( idx = 2; idx <= ${#commands[@]}; idx++ )); do
        pane_id=$(tmux split-window -h -c ~ -t $SESSION_NAME:"$window_name" -P -F '#{pane_id}') || return 1
        tmux send-keys -t "$pane_id" "$PANE_SETUP_CMD && ${commands[$idx]}" C-m || return 1
    done

    if [ ${#commands[@]} -gt 1 ]; then
        tmux select-layout -t $SESSION_NAME:"$window_name" even-horizontal
    fi

    sleep 0.1
    return 0
}

for i in $(seq 1 ${#ROS_COMMAND_GROUPS[@]}); do
    process_command_group $i || { 
        echo "ERROR: Failed to create window $i" >&2
        tmux kill-session -t $SESSION_NAME 2>/dev/null
        exit 1
    }
done

tmux select-window -t $SESSION_NAME:"${WINDOW_NAMES[1]}"

WEBAPP_URI=""
if typeset -f innate_webapp_uri >/dev/null 2>&1; then
    WEBAPP_URI="$(innate_webapp_uri)"
fi
SESSION_FMT=$(printf "%-29s" "$SESSION_NAME")
WINDOWS_FMT=$(printf "%-29s" "${#WINDOW_NAMES[@]}")
ATTACH_FMT=$(printf "%-29s" "tmux attach -t $SESSION_NAME")

echo ""
echo "  ╔═════════════════════════════════════════╗     ____"
echo "  ║  All systems launched ✅                ║    [O  O]"
echo "  ║                                         ║     _||_"
echo "  ║  Session:  ${SESSION_FMT}║   |      |"
echo "  ║  Windows:  ${WINDOWS_FMT}║   |______|"
echo "  ║  Attach:   ${ATTACH_FMT}║     o  o"
echo "  ║                                         ║"
echo "  ║  Run 'innate view' to monitor nodes 👀  ║"
echo "  ╚═════════════════════════════════════════╝"
if [ -n "$WEBAPP_URI" ]; then
    echo "  Web app: $WEBAPP_URI"
fi
echo ""

# Loud, hard-to-miss warning when the robot has no service key. Printed last so
# it's the final thing on screen. Without a key the robot still boots, but it
# can't reach the Innate cloud brain — AI behaviors, logging and training are
# all dead until a key is configured.
if [ "$SERVICE_KEY_MISSING" = true ]; then
    printf '\033[1;31m'
    echo "  ════════════════════════════════════════════════════════════════════"
    echo "   ⚠  NO SERVICE KEY FOUND"
    echo "  ════════════════════════════════════════════════════════════════════"
    echo "   This robot has no INNATE_SERVICE_KEY, so it cannot connect to the"
    echo "   Innate cloud brain. AI behaviors, logging, and on-device training"
    echo "   will not work until a service key is configured."
    echo ""
    echo "   To get a new service key, contact Innate on Discord:"
    echo "       https://discord.gg/innate"
    echo "  ════════════════════════════════════════════════════════════════════"
    printf '\033[0m'
    echo ""
fi

# Play ten seconds after launch, past the worst of the node import storm
# (backgrounded). SCHED_FIFO so that storm can't starve the player past dmix's
# ~85 ms buffer (audible pops); rtprio 30 stays under zenoh's watchdog at 48.
(
    sleep 10
    rt=()
    chrt -f 30 true 2>/dev/null && rt=(chrt -f 30)
    "${rt[@]}" gst-play-1.0 \
        "$INNATE_OS_ROOT/config/sounds/turnon.mp3" >/dev/null 2>&1
) &
disown

# Every pane has sourced the env file by now, so drop it: the service key should
# not linger in /tmp. Done in the background because `exec sleep infinity` below
# never returns, so the EXIT trap can't remove it on the normal path.
(sleep 15 && rm -f "$PANE_SETUP_FILE") &
disown

# Keep the script alive so systemd (Type=simple) considers the service running.
# When systemctl stop is called, SIGTERM kills this sleep, then ExecStop cleans up tmux.
exec sleep infinity
