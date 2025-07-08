#!/bin/zsh
# This script sources the necessary ROS/DDS environment and launches multiple
# ROS launch commands, grouped into windows with 2 services per window,
# managed by systemd. The script itself stays alive to monitor the tmux session.

# --- Configuration ---
SESSION_NAME="ros_nodes"
# !! IMPORTANT: Set these paths correctly for your system !!
ROS_WS_PATH="$HOME/maurice-prod/ros2_ws" # Path to your ROS workspace
DDS_SETUP_SCRIPT="$HOME/maurice-prod/dds/setup_dds.zsh" # Path to the DDS setup script

# Define the ROS launch commands to run, grouped into windows (ZSH arrays are 1-based)
# Each group will be a window with 2 panes (or 1 if odd number)
ROS_COMMAND_GROUPS=(
    # Group 1: Control & Bringup
    "ros2 launch maurice_control app.launch.py|ros2 launch maurice_bringup maurice_bringup.launch.py"
    # Group 2: Arm & Recorder
    "ros2 launch maurice_arm arm.launch.py|ros2 launch manipulation recorder.launch.py"
    # Group 3: Brain Client & Navigation Manager
    "sleep 15 && ros2 service call /maurice_arm/goto_js maurice_msgs/srv/GotoJS '{data: {data: [1.57693225, -0.6, 1.4772235, -0.73784476, 0.0, 0.91425255]}, time: 5}' && ros2 launch brain_client brain_client.launch.py|sleep 5 && ros2 service call /calibrate std_srvs/srv/Trigger && sleep 5 && ros2 launch maurice_nav mode_manager.launch.py"
    # Group 4: Behavior (single command)
    "ros2 launch manipulation behavior.launch.py"
)

# Define window names for better organization
WINDOW_NAMES=(
    "control-bringup"
    "arm-recorder"
    "brain-nav"
    "behavior"
)
# ------

# Define the command to keep panes open
PANE_EXIT_CMD="echo 'ROS Launch finished or exited. Press Enter to close this tmux pane...' ; read"

# Construct the command parts for sourcing environment inside tmux
DDS_SOURCE_CMD="source $DDS_SETUP_SCRIPT"
ROS_SOURCE_CMD="source $ROS_WS_PATH/install/setup.zsh"

echo "Attempting to launch ROS commands in tmux session '$SESSION_NAME'..."

# Add a 10-second delay before starting
echo "Sleeping for 10 seconds before starting..."
sleep 10

# Source environment *before* tmux as well (might help tmux itself)
echo "Sourcing DDS setup (pre-tmux): $DDS_SETUP_SCRIPT"
source "$DDS_SETUP_SCRIPT"
if [ $? -ne 0 ]; then echo "ERROR: Sourcing DDS setup script failed (pre-tmux)." >&2; exit 1; fi
echo "ROS_DISCOVERY_SERVER_IP is now set to: $ROS_DISCOVERY_SERVER_IP"

echo "Sourcing ROS workspace setup (pre-tmux): $ROS_WS_PATH/install/setup.zsh"
if [ -f "$ROS_WS_PATH/install/setup.zsh" ]; then
    source "$ROS_WS_PATH/install/setup.zsh"
    if [ $? -ne 0 ]; then echo "ERROR: Sourcing ROS workspace setup failed (pre-tmux)." >&2; exit 1; fi
else
    echo "ERROR: ROS workspace setup file not found at $ROS_WS_PATH/install/setup.zsh" >&2
    exit 1
fi

# Check if there are any command groups defined
if [ ${#ROS_COMMAND_GROUPS[@]} -eq 0 ]; then
    echo "ERROR: No ROS_COMMAND_GROUPS defined in the script." >&2
    exit 1
fi

# Kill existing session if it exists (ensures fresh start on service restart)
if tmux has-session -t $SESSION_NAME 2>/dev/null; then
    echo "Existing tmux session '$SESSION_NAME' found. Killing it."
    tmux kill-session -t $SESSION_NAME
    sleep 1 # Give it a moment to die gracefully
fi

# Function to process a command group and create window with panes
process_command_group() {
    local group_index=$1
    local command_group="${ROS_COMMAND_GROUPS[$group_index]}"
    local window_name="${WINDOW_NAMES[$group_index]}"
    
    # Split commands by pipe delimiter
    local commands=(${(s:|:)command_group})
    
    echo "Creating window '$window_name' with ${#commands[@]} command(s)..."
    
    if [ $group_index -eq 1 ]; then
        # Create the session with the first window
        tmux new-session -d -s $SESSION_NAME -n "$window_name" -c ~
        if [ $? -ne 0 ]; then
            echo "ERROR: Failed to create tmux session '$SESSION_NAME'." >&2
            return 1
        fi
    else
        # Create additional windows
        tmux new-window -t $SESSION_NAME -n "$window_name" -c ~
        if [ $? -ne 0 ]; then
            echo "ERROR: Failed to create window '$window_name'." >&2
            return 1
        fi
    fi
    
    sleep 0.5 # Give window time to be created
    
    # Send first command to the main pane
    local first_cmd="${commands[1]}"
    local first_cmd_full="$DDS_SOURCE_CMD && $ROS_SOURCE_CMD && $first_cmd ; $PANE_EXIT_CMD"
    echo "  Pane 0: $first_cmd"
    tmux send-keys -t $SESSION_NAME:"$window_name".0 "$first_cmd_full" C-m
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to send command to pane 0 in window '$window_name'." >&2
        return 1
    fi
    
    # If there's a second command, create a second pane
    if [ ${#commands[@]} -gt 1 ]; then
        local second_cmd="${commands[2]}"
        local second_cmd_full="$DDS_SOURCE_CMD && $ROS_SOURCE_CMD && $second_cmd ; $PANE_EXIT_CMD"
        
        echo "  Pane 1: $second_cmd"
        tmux split-window -h -c ~ -t $SESSION_NAME:"$window_name"
        if [ $? -ne 0 ]; then
            echo "ERROR: Failed to create second pane in window '$window_name'." >&2
            return 1
        fi
        
        sleep 0.5 # Give pane time to be created
        tmux send-keys -t $SESSION_NAME:"$window_name".1 "$second_cmd_full" C-m
        if [ $? -ne 0 ]; then
            echo "ERROR: Failed to send command to pane 1 in window '$window_name'." >&2
            return 1
        fi
        
        # Set a nice layout for the two panes
        tmux select-layout -t $SESSION_NAME:"$window_name" even-horizontal
    fi
    
    sleep 0.5 # Small delay between windows
    return 0
}

# Process all command groups
for i in $(seq 1 ${#ROS_COMMAND_GROUPS[@]}); do
    process_command_group $i
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to process command group $i." >&2
        tmux kill-session -t $SESSION_NAME 2>/dev/null
        exit 1
    fi
done

# Select the first window by default
tmux select-window -t $SESSION_NAME:"${WINDOW_NAMES[1]}"

echo "Successfully launched ROS commands in detached tmux session '$SESSION_NAME'."
echo "Windows created:"
echo "  0: ${WINDOW_NAMES[1]} (control & bringup)"
echo "  1: ${WINDOW_NAMES[2]} (arm & recorder)" 
echo "  2: ${WINDOW_NAMES[3]} (brain client & navigation)"
echo "  3: ${WINDOW_NAMES[4]} (behavior)"
echo ""
echo "Attach with: tmux attach -t $SESSION_NAME"
echo "Navigate between windows: Ctrl+B then 0-3, or Ctrl+B + n/p"
echo "Navigate between panes in a window: Ctrl+B then arrow keys"

# Keep the script running as long as the tmux session exists
echo "Monitoring tmux session '$SESSION_NAME'. Script will exit when session ends."
while tmux has-session -t $SESSION_NAME 2>/dev/null; do
    sleep 5 # Check every 5 seconds
done

echo "Tmux session '$SESSION_NAME' ended. Exiting monitoring script."
exit 0 