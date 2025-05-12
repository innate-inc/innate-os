#!/bin/zsh
# This script sources the necessary ROS/DDS environment and launches multiple
# ROS launch commands, each in its own pane within a detached tmux session,
# managed by systemd. The script itself stays alive to monitor the tmux session.

# --- Configuration ---
SESSION_NAME="ros_nodes"
# !! IMPORTANT: Set these paths correctly for your system !!
ROS_WS_PATH="$HOME/maurice-prod/ros2_ws" # Path to your ROS workspace
DDS_SETUP_SCRIPT="$HOME/maurice-prod/dds/setup_dds.zsh" # Path to the DDS setup script

# Define the ROS launch commands to run, each in its own pane (ZSH arrays are 1-based)
ROS_LAUNCH_COMMANDS=(
    "ros2 launch maurice_control app.launch.py"
    "ros2 launch maurice_bringup maurice_bringup.launch.py"
    "ros2 launch maurice_arm arm.launch.py"
    "sleep 15 && ros2 service call /maurice_arm/goto_js maurice_msgs/srv/GotoJS '{data: {data: [0.8528933180644165, -0.45712627478992107, 1.2946797849754812, -0.9326603190344698, -0.04908738521234052, 0.8881748761857863]}, time: 5}'"
    "sleep 10 && ros2 service call /calibrate std_srvs/srv/Trigger && ros2 launch maurice_navigation navigation.launch.py"
)
# ------

# Define the command to keep panes open
PANE_EXIT_CMD="echo 'ROS Launch finished or exited. Press Enter to close this tmux pane...' ; read"

# Construct the command parts for sourcing environment inside tmux
# Use single quotes for send-keys and escape internal quotes/variables carefully if needed
# Relying on the initial source might be enough if tmux inherits correctly, but being explicit is safer.
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

# Check if there are any commands defined
if [ ${#ROS_LAUNCH_COMMANDS[@]} -eq 0 ]; then
    echo "ERROR: No ROS_LAUNCH_COMMANDS defined in the script." >&2
    exit 1
fi

# Kill existing session if it exists (ensures fresh start on service restart)
if tmux has-session -t $SESSION_NAME 2>/dev/null; then
    echo "Existing tmux session '$SESSION_NAME' found. Killing it."
    tmux kill-session -t $SESSION_NAME
    sleep 1 # Give it a moment to die gracefully
fi

# Create new detached session, starting with just a shell
echo "Creating new detached tmux session '$SESSION_NAME'..."
tmux new-session -d -s $SESSION_NAME -c ~ # Start default shell

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to create tmux session '$SESSION_NAME'." >&2
    exit 1
fi
sleep 0.5 # Give tmux a moment to settle

# Send the first command to the initial pane (0)
# Use 1-based indexing for zsh arrays
FIRST_CMD_RAW="${ROS_LAUNCH_COMMANDS[1]}"
if [ -z "$FIRST_CMD_RAW" ]; then
    echo "ERROR: First command in ROS_LAUNCH_COMMANDS is empty or not defined." >&2
    tmux kill-session -t $SESSION_NAME # Clean up session
    exit 1
fi
FIRST_CMD_FULL="$DDS_SOURCE_CMD && $ROS_SOURCE_CMD && $FIRST_CMD_RAW ; $PANE_EXIT_CMD"
echo "Sending command to pane 0: $FIRST_CMD_RAW"
# Use single quotes to simplify sending the command string via send-keys
tmux send-keys -t $SESSION_NAME:0.0 "$FIRST_CMD_FULL" C-m
if [ $? -ne 0 ]; then
    echo "ERROR: Failed send command to initial pane for '$FIRST_CMD_RAW'." >&2
    tmux kill-session -t $SESSION_NAME # Clean up session
    exit 1
fi
sleep 0.5 # Small delay

# Add remaining commands in new panes (start loop from 2)
for i in $(seq 2 ${#ROS_LAUNCH_COMMANDS[@]}); do
    CMD_RAW="${ROS_LAUNCH_COMMANDS[$i]}"
    CMD_FULL="$DDS_SOURCE_CMD && $ROS_SOURCE_CMD && $CMD_RAW ; $PANE_EXIT_CMD"
    PANE_INDEX=$((i - 1)) # Pane index is 0-based (0, 1, 2...)

    echo "Creating pane $PANE_INDEX and sending command: $CMD_RAW"
    tmux split-window -h -c ~ -t $SESSION_NAME:0 # Create a new horizontal pane
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed to split window for pane $PANE_INDEX." >&2
        continue # Try next command
    fi
    sleep 0.5 # Give pane time to be created

    # Send the command to the newly created pane
    # Target the correct pane using 0-based index
    tmux send-keys -t $SESSION_NAME:0.$PANE_INDEX "$CMD_FULL" C-m
    if [ $? -ne 0 ]; then
        echo "ERROR: Failed send command to pane $PANE_INDEX for '$CMD_RAW'." >&2
        # Optionally kill the session here or just report error
    fi
    sleep 0.5 # Small delay between starting panes
done

# Select a default layout (optional, e.g., tiled)
tmux select-layout -t $SESSION_NAME:0 tiled

echo "Successfully launched ROS commands in detached tmux session '$SESSION_NAME'."
echo "Attach with: tmux attach -t $SESSION_NAME"

# Keep the script running as long as the tmux session exists
echo "Monitoring tmux session '$SESSION_NAME'. Script will exit when session ends."
while tmux has-session -t $SESSION_NAME 2>/dev/null; do
    sleep 5 # Check every 5 seconds
done

echo "Tmux session '$SESSION_NAME' ended. Exiting monitoring script."
exit 0 