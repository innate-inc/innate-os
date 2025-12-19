#!/bin/zsh
# Launch ROS nodes as background processes

ROS_WS_PATH="$INNATE_OS_ROOT/ros2_ws"
DDS_SETUP_SCRIPT="$INNATE_OS_ROOT/dds/setup_dds.zsh"
LOG_DIR="/tmp/ros_launch_logs"

# ROS launch commands (one per window)
ROS_COMMANDS=(
    "timeout 60 ros2 topic echo /rosout > /tmp/rosout"
    "ros2 launch maurice_control app.launch.py"
    "ros2 launch maurice_bringup maurice_bringup.launch.py"
    "ros2 launch maurice_arm arm.launch.py"
    "ros2 launch manipulation recorder.launch.py"
    "ros2 launch brain_client brain_client.launch.py"
    #"sleep 5 && ros2 service call /calibrate std_srvs/srv/Trigger && sleep 5 && ros2 launch maurice_nav mode_manager.launch.py"
    "ros2 service call /calibrate std_srvs/srv/Trigger && sleep 1 && ros2 launch maurice_nav mode_manager.launch.py"
    "ros2 launch manipulation behavior.launch.py"
    "ros2 launch brain_client input_manager.launch.py"
    "ros2 launch innate_webrtc_streamer webrtc_streamer.launch.py"
    "ros2 launch maurice_control udp_leader_receiver.launch.py"
    "ros2 run maurice_arm ik.py"
    "ros2 launch maurice_log logger.launch.py"
)

WINDOW_NAMES=(
    "rosout"
    "maurice-control"
    "maurice-bringup"
    "maurice-arm"
    "manipulation-recorder"
    "brain-client"
    "calibrate-nav"
    "manipulation-behavior"
    "input-manager"
    "webrtc-streamer"
    "udp-leader"
    "ik"
    "logger"
)

echo "Launching ROS nodes as background processes..."

# Create log directory
mkdir -p "$LOG_DIR"

# Source environment once (will be inherited by all child processes)
source "$DDS_SETUP_SCRIPT" || { echo "ERROR: Failed to source DDS setup." >&2; exit 1; }

if [ -f "$ROS_WS_PATH/install/setup.zsh" ]; then
    source "$ROS_WS_PATH/install/setup.zsh" || { echo "ERROR: Failed to source ROS workspace." >&2; exit 1; }
else
    echo "ERROR: ROS workspace setup not found at $ROS_WS_PATH/install/setup.zsh" >&2
    exit 1
fi

# Track all child process PIDs
declare -a PIDS

process_command() {
    local cmd_index=$1
    local command="${ROS_COMMANDS[$cmd_index]}"
    local window_name="${WINDOW_NAMES[$cmd_index]}"
    local log_file="$LOG_DIR/${window_name}.log"
    
    echo "  Starting: $window_name"
    
    # Run command in background, redirecting output to log file
    eval "$command" > "$log_file" 2>&1 &
    
    local pid=$!
    PIDS[$cmd_index]=$pid
    echo "    PID: $pid -> $log_file"
}

echo "Starting all ROS nodes..."
for i in $(seq 1 ${#ROS_COMMANDS[@]}); do
    process_command $i
done

echo ""
echo "✓ All ROS nodes launched (${#ROS_COMMANDS[@]} processes)"
echo "  Log directory: $LOG_DIR"
echo "  View logs: tail -f $LOG_DIR/*.log"
echo "  Kill all: pkill -P $$ 2>/dev/null"
echo ""

# Wait for all child processes
wait 
