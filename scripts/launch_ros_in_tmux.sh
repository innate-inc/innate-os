#!/bin/zsh
# This script sources the necessary ROS/DDS environment and launches the main
# ROS application within a detached tmux session, managed by systemd.
# The script itself stays alive to monitor the tmux session.

# --- Configuration --- 
SESSION_NAME="ros_nodes"
# !! IMPORTANT: Set these paths correctly for your system !!
ROS_LAUNCH_PACKAGE="maurice_control" # Replace with your actual ROS launch package
ROS_LAUNCH_FILE="app.launch.py" # Replace with your actual ROS launch file
ROS_WS_PATH="$HOME/maurice-prod/ros2_ws" # Path to your ROS workspace
DDS_SETUP_SCRIPT="$HOME/maurice-prod/dds/setup_dds.zsh" # Path to the DDS setup script
# ------

ROS_LAUNCH_COMMAND="ros2 launch $ROS_LAUNCH_PACKAGE $ROS_LAUNCH_FILE"

echo "Attempting to launch ROS in tmux session '$SESSION_NAME'..."

# Source environment (CRITICAL for dynamic IP and ROS paths)
echo "Sourcing DDS setup: $DDS_SETUP_SCRIPT"
source "$DDS_SETUP_SCRIPT"
if [ $? -ne 0 ]; then echo "ERROR: Sourcing DDS setup script failed." >&2; exit 1; fi
echo "ROS_DISCOVERY_SERVER_IP is now set to: $ROS_DISCOVERY_SERVER_IP"

echo "Sourcing ROS workspace setup: $ROS_WS_PATH/install/setup.zsh"
if [ -f "$ROS_WS_PATH/install/setup.zsh" ]; then
    source "$ROS_WS_PATH/install/setup.zsh"
    if [ $? -ne 0 ]; then echo "ERROR: Sourcing ROS workspace setup failed." >&2; exit 1; fi
else
    echo "ERROR: ROS workspace setup file not found at $ROS_WS_PATH/install/setup.zsh" >&2
    exit 1
fi

# Kill existing session if it exists (ensures fresh start on service restart)
if tmux has-session -t $SESSION_NAME 2>/dev/null; then
    echo "Existing tmux session '$SESSION_NAME' found. Killing it."
    tmux kill-session -t $SESSION_NAME
    sleep 1 # Give it a moment to die gracefully
fi

# Create new detached session and run the launch command
# The 'read' command at the end keeps the window open after launch finishes/crashes
# for inspection when attached manually. Remove if you want it to close immediately.
echo "Creating new detached tmux session '$SESSION_NAME' and launching: $ROS_LAUNCH_COMMAND"
tmux new-session -d -s $SESSION_NAME \
    "$ROS_LAUNCH_COMMAND ; echo 'ROS Launch finished or exited.' ; read -p 'Press Enter to close this tmux pane...'"

if [ $? -eq 0 ]; then
    echo "Successfully started ROS launch in detached tmux session '$SESSION_NAME'."
    echo "Attach with: tmux attach -t $SESSION_NAME"
else
    echo "ERROR: Failed to create tmux session or run ROS launch command." >&2
    exit 1
fi

# Keep the script running as long as the tmux session exists
echo "Monitoring tmux session '$SESSION_NAME'. Script will exit when session ends."
while tmux has-session -t $SESSION_NAME 2>/dev/null; do
    sleep 5 # Check every 5 seconds
done

echo "Tmux session '$SESSION_NAME' ended. Exiting monitoring script."
exit 0 