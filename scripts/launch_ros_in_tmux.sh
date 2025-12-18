#!/bin/bash
# Launch ROS nodes in tmux windows with 2 panes each
# Ensures discovery server is running and DDS is properly configured

SESSION_NAME="ros_nodes"

# Auto-detect INNATE_OS_ROOT if not set
if [ -z "$INNATE_OS_ROOT" ]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    INNATE_OS_ROOT="$(dirname "$SCRIPT_DIR")"
fi
export INNATE_OS_ROOT

ROS_WS_PATH="$INNATE_OS_ROOT/ros2_ws"
DDS_SETUP_SCRIPT="$INNATE_OS_ROOT/dds/setup_dds.sh"

# ROS launch commands grouped into windows (pipe-delimited for 2 panes)
ROS_COMMAND_GROUPS=(
    "ros2 launch maurice_control app.launch.py|ros2 launch maurice_bringup maurice_bringup.launch.py"
    "ros2 launch maurice_arm arm.launch.py|ros2 launch manipulation recorder.launch.py"
    "ros2 launch brain_client brain_client.launch.py|sleep 5 && ros2 service call /calibrate std_srvs/srv/Trigger && sleep 5 && ros2 launch maurice_nav mode_manager.launch.py"
    "ros2 launch manipulation behavior.launch.py|ros2 launch brain_client input_manager.launch.py"
    "ros2 launch innate_webrtc_streamer webrtc_streamer.launch.py|ros2 launch maurice_control udp_leader_receiver.launch.py"
    "ros2 run maurice_arm ik.py|ros2 launch maurice_log logger.launch.py"
)

WINDOW_NAMES=(
    "app-bringup"
    "arm-recorder"
    "brain-nav"
    "behaviors-inputs"
    "stream"
    "ik-logger"
)

echo "Launching ROS nodes in tmux session '$SESSION_NAME'..."
echo "  INNATE_OS_ROOT: $INNATE_OS_ROOT"
echo "  ROS Workspace: $ROS_WS_PATH"

# -----------------------------------------------------------------------------
# 1. Ensure Discovery Server is running
# -----------------------------------------------------------------------------
echo "Checking FastDDS Discovery Server..."

# Check if discovery-server systemd service is running
if systemctl is-active --quiet discovery-server.service 2>/dev/null; then
    echo "  Discovery server (systemd) is running"
else
    # Try to start via systemd
    if systemctl start discovery-server.service 2>/dev/null; then
        echo "  Started discovery server via systemd"
        sleep 2
    else
        # Fall back to tmux-based discovery server
        if ! tmux has-session -t discovery 2>/dev/null; then
            echo "  Starting discovery server in tmux..."
            tmux new-session -d -s discovery
            tmux send-keys -t discovery "source /opt/ros/humble/setup.bash && fastdds discovery -i 0 -p 11811" C-m
            sleep 2
            echo "  Discovery server started in tmux session 'discovery'"
        else
            echo "  Discovery server (tmux) is already running"
        fi
    fi
fi

# -----------------------------------------------------------------------------
# 2. Setup DDS environment
# -----------------------------------------------------------------------------
echo "Configuring DDS..."

# Source ROS base
source /opt/ros/humble/setup.bash

# Source DDS setup to configure environment and generate XML config
if [ -f "$DDS_SETUP_SCRIPT" ]; then
    source "$DDS_SETUP_SCRIPT"
    echo "  FASTRTPS_DEFAULT_PROFILES_FILE: $FASTRTPS_DEFAULT_PROFILES_FILE"
    echo "  ROS_DISCOVERY_SERVER: $ROS_DISCOVERY_SERVER"
else
    echo "  WARNING: DDS setup script not found at $DDS_SETUP_SCRIPT"
fi

# Source workspace
if [ -f "$ROS_WS_PATH/install/setup.bash" ]; then
    source "$ROS_WS_PATH/install/setup.bash"
else
    echo "ERROR: ROS workspace setup not found at $ROS_WS_PATH/install/setup.bash" >&2
    exit 1
fi

# -----------------------------------------------------------------------------
# 3. Verify DDS configuration was generated correctly
# -----------------------------------------------------------------------------
echo "Verifying DDS configuration..."
if [ ! -f "$FASTRTPS_DEFAULT_PROFILES_FILE" ]; then
    echo "ERROR: DDS configuration file not generated!" >&2
    exit 1
fi

# Show the generated config for debugging
echo "  Generated DDS config:"
grep -E "(address|port)" "$FASTRTPS_DEFAULT_PROFILES_FILE" | head -4

# -----------------------------------------------------------------------------
# 4. Create the setup script that each pane will source
# -----------------------------------------------------------------------------
# We create a temporary script that sets up the full environment
# This ensures all env vars are properly set in each tmux pane
# NOTE: We use bash explicitly to avoid zsh/bash compatibility issues with ROS setup scripts

PANE_SETUP_SCRIPT="/tmp/innate_ros_setup_$$.sh"
cat > "$PANE_SETUP_SCRIPT" << SETUP_EOF
#!/bin/bash
export INNATE_OS_ROOT="$INNATE_OS_ROOT"
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_DOMAIN_ID=0
export ROS_DISCOVERY_SERVER_IP="$ROS_DISCOVERY_SERVER_IP"
export ROS_DISCOVERY_SERVER_PORT="$ROS_DISCOVERY_SERVER_PORT"
export ROS_DISCOVERY_SERVER="$ROS_DISCOVERY_SERVER"
export FASTRTPS_DEFAULT_PROFILES_FILE="$FASTRTPS_DEFAULT_PROFILES_FILE"

source /opt/ros/humble/setup.bash
source "$ROS_WS_PATH/install/setup.bash"

echo "DDS configured: \$ROS_DISCOVERY_SERVER"
echo "Config file: \$FASTRTPS_DEFAULT_PROFILES_FILE"
SETUP_EOF
chmod +x "$PANE_SETUP_SCRIPT"

# -----------------------------------------------------------------------------
# 5. Kill existing session if present
# -----------------------------------------------------------------------------
if tmux has-session -t $SESSION_NAME 2>/dev/null; then
    echo "Killing existing tmux session..."
    tmux kill-session -t $SESSION_NAME
    sleep 1
fi

# -----------------------------------------------------------------------------
# 6. Create tmux windows and panes
# -----------------------------------------------------------------------------
process_command_group() {
    local group_index=$1
    local command_group="${ROS_COMMAND_GROUPS[$group_index]}"
    local window_name="${WINDOW_NAMES[$group_index]}"

    # Split by pipe delimiter
    IFS='|' read -ra commands <<< "$command_group"

    echo "  Creating window: $window_name"

    # Create windows/panes with bash as the shell to avoid zsh compatibility issues
    if [ $group_index -eq 0 ]; then
        tmux new-session -d -s $SESSION_NAME -n "$window_name" -c "$INNATE_OS_ROOT" "bash" || return 1
    else
        tmux new-window -t $SESSION_NAME -n "$window_name" -c "$INNATE_OS_ROOT" "bash" || return 1
    fi

    sleep 0.3

    # First pane - source setup and run command
    local first_cmd="${commands[0]}"
    tmux send-keys -t $SESSION_NAME:"$window_name".0 "source $PANE_SETUP_SCRIPT && $first_cmd" C-m || return 1

    # Second pane (if present)
    if [ ${#commands[@]} -gt 1 ]; then
        local second_cmd="${commands[1]}"

        # Split and start bash in the new pane
        tmux split-window -h -c "$INNATE_OS_ROOT" -t $SESSION_NAME:"$window_name" "bash" || return 1
        sleep 0.3
        tmux send-keys -t $SESSION_NAME:"$window_name".1 "source $PANE_SETUP_SCRIPT && $second_cmd" C-m || return 1
        tmux select-layout -t $SESSION_NAME:"$window_name" even-horizontal
    fi

    sleep 0.2
    return 0
}

# Create all windows
for i in $(seq 0 $((${#ROS_COMMAND_GROUPS[@]} - 1))); do
    process_command_group $i || {
        echo "ERROR: Failed to create window $i" >&2
        tmux kill-session -t $SESSION_NAME 2>/dev/null
        rm -f "$PANE_SETUP_SCRIPT"
        exit 1
    }
done

tmux select-window -t $SESSION_NAME:"${WINDOW_NAMES[0]}"

echo ""
echo "============================================"
echo "  ROS nodes launched in tmux session '$SESSION_NAME'"
echo "============================================"
echo ""
echo "  Discovery Server: $ROS_DISCOVERY_SERVER"
echo "  DDS Config: $FASTRTPS_DEFAULT_PROFILES_FILE"
echo ""
echo "  Attach: tmux attach -t $SESSION_NAME"
echo "  Discovery: tmux attach -t discovery (if using tmux discovery)"
echo ""

# Wait for session to end
while tmux has-session -t $SESSION_NAME 2>/dev/null; do
    sleep 5
done

# Cleanup
rm -f "$PANE_SETUP_SCRIPT"
echo "Tmux session ended."
