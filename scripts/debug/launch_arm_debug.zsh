#!/bin/zsh
# launch_arm_debug.zsh
# Arm debug setup: hot reload, foxglove bridge, goto_pose nav, and arm node
# Usage: ./scripts/debug/launch_arm_debug.zsh

SESSION_NAME="arm_debug"
ROS_WS_PATH="$INNATE_OS_ROOT/ros2_ws"
DDS_SETUP_SCRIPT="$INNATE_OS_ROOT/scripts/rmw/setup_dds.zsh"
PID_HOT_RELOAD="$ROS_WS_PATH/src/maurice_bot/maurice_arm/maurice_arm/pid_hot_reload.py"
ARM_GOTO_LOOP="$INNATE_OS_ROOT/scripts/debug/arm_goto_loop.py"

DDS_SOURCE_CMD="source $DDS_SETUP_SCRIPT"
ROS_SOURCE_CMD="source $ROS_WS_PATH/install/setup.zsh"
ENV_CMD="$DDS_SOURCE_CMD && $ROS_SOURCE_CMD"

echo "🦾 Launching arm debug session '$SESSION_NAME'..."

# Source environment
source "$DDS_SETUP_SCRIPT" || { echo "ERROR: Failed to source DDS setup." >&2; exit 1; }
source "$ROS_WS_PATH/install/setup.zsh" || { echo "ERROR: Failed to source ROS workspace." >&2; exit 1; }

# Kill existing session
tmux kill-session -t ${SESSION_NAME} 2>/dev/null
sleep 0.5

# === Window 1: Arm node ===
tmux new-session -d -s ${SESSION_NAME} -n arm -c ~
tmux send-keys -t ${SESSION_NAME}:arm "$ENV_CMD && ros2 launch maurice_arm arm.launch.py" C-m
echo "  ✓ arm node"

# === Window 2: PID Hot Reload ===
tmux new-window -t ${SESSION_NAME} -n hot-reload -c ~
tmux send-keys -t ${SESSION_NAME}:hot-reload "$ENV_CMD && python3 $PID_HOT_RELOAD" C-m
echo "  ✓ pid hot reload"

# === Window 3: Foxglove Bridge ===
tmux new-window -t ${SESSION_NAME} -n foxglove -c ~
tmux send-keys -t ${SESSION_NAME}:foxglove "$ENV_CMD && ros2 launch foxglove_bridge foxglove_bridge_launch.xml" C-m
echo "  ✓ foxglove bridge (ws://localhost:8765)"

# === Window 4: Goto Pose Loop ===
tmux new-window -t ${SESSION_NAME} -n goto-loop -c ~
tmux send-keys -t ${SESSION_NAME}:goto-loop "$ENV_CMD && python3 $ARM_GOTO_LOOP" C-m
echo "  ✓ arm goto pose loop"

# Select the arm window by default
tmux select-window -t ${SESSION_NAME}:arm

echo ""
echo "✓ Arm debug session launched in tmux session '$SESSION_NAME'"
echo "  Attach: tmux attach -t $SESSION_NAME"
echo ""
echo "  Windows:"
echo "    0: arm          – arm.launch.py (arm + MoveIt planning)"
echo "    1: hot-reload   – PID hot reload watcher"
echo "    2: foxglove     – Foxglove bridge on ws://localhost:8765"
echo "    3: goto-loop    – Arm goto pose loop (edit poses in arm_goto_loop.py)"
echo ""

tmux attach-session -t ${SESSION_NAME}
