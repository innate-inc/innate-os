#!/bin/zsh
# Launch ROS nodes in tmux windows with 2 panes each

SESSION_NAME="ros_nodes"
ROS_WS_PATH="$INNATE_OS_ROOT/ros2_ws"
DDS_SETUP_SCRIPT="$INNATE_OS_ROOT/dds/setup_dds.zsh"

# ROS launch commands grouped into windows (pipe-delimited for 2 panes)
ROS_COMMAND_GROUPS=(
    "ros2 launch maurice_control app.launch.py|ros2 launch maurice_bringup maurice_bringup.launch.py"
    "ros2 launch maurice_arm arm.launch.py|ros2 launch manipulation recorder.launch.py"
    "ros2 launch brain_client brain_client.launch.py|sleep 5 && ros2 service call /calibrate std_srvs/srv/Trigger && sleep 5 && ros2 launch maurice_nav mode_manager.launch.py"
    "ros2 launch manipulation behavior.launch.py|ros2 launch brain_client input_manager.launch.py"
    "ros2 launch innate_webrtc_streamer webrtc_streamer.launch.py|ros2 launch maurice_control udp_leader_receiver.launch.py"
)

WINDOW_NAMES=(
    "control-bringup"
    "arm-recorder"
    "brain-nav"
    "behaviors-inputs"
    "stream"
)

DDS_SOURCE_CMD="source $DDS_SETUP_SCRIPT"
ROS_SOURCE_CMD="source $ROS_WS_PATH/install/setup.zsh"

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
    
    local first_cmd="${commands[1]}"
    local first_cmd_full="$DDS_SOURCE_CMD && $ROS_SOURCE_CMD && $first_cmd"
    tmux send-keys -t $SESSION_NAME:"$window_name".0 "$first_cmd_full" C-m || return 1
    
    if [ ${#commands[@]} -gt 1 ]; then
        local second_cmd="${commands[2]}"
        local second_cmd_full="$DDS_SOURCE_CMD && $ROS_SOURCE_CMD && $second_cmd"
        
        tmux split-window -h -c ~ -t $SESSION_NAME:"$window_name" || return 1
        sleep 0.1
        tmux send-keys -t $SESSION_NAME:"$window_name".1 "$second_cmd_full" C-m || return 1
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

echo "✓ ROS nodes launched in tmux session '$SESSION_NAME'"
echo "  Attach: tmux attach -t $SESSION_NAME"
echo ""
while tmux has-session -t $SESSION_NAME 2>/dev/null; do
    sleep 5
done

echo "Tmux session ended." 