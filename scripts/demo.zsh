#!/bin/zsh -l

# Initial delay of 20 seconds
sleep 60

# Create a new tmux session named "discovery" with fastdds as the initial window
tmux new-session -d -s discovery -n fastdds
tmux send-keys -t discovery:fastdds "fastdds discovery -i 0 -p 11811" C-m

sleep 5
# --- Window for Arm node ---
tmux new-window -t discovery -n arm
tmux send-keys -t discovery:arm "ros2 launch maurice_arm arm.launch.py" C-m

# --- Window for Control node ---
tmux new-window -t discovery -n control
tmux send-keys -t discovery:control "ros2 launch maurice_control app.launch.py" C-m

# --- Window for Bringup node ---
tmux new-window -t discovery -n bringup
tmux send-keys -t discovery:bringup "ros2 launch maurice_bringup bringup_core.launch.py" C-m

# --- Window for Camera node ---
tmux new-window -t discovery -n camera
tmux send-keys -t discovery:camera "ros2 launch maurice_bringup camera.launch.py" C-m

# --- Window for Recorder node ---
tmux new-window -t discovery -n recorder
tmux send-keys -t discovery:recorder "ros2 launch manipulation recorder.launch.py" C-m

# --- Window for Service Call ---
sleep 20
tmux new-window -t discovery -n service
tmux send-keys -t discovery:service "ros2 service call /maurice_arm/goto_js maurice_msgs/srv/GotoJS '{data: {data: [0.8528933180644165, -0.45712627478992107, 1.2946797849754812, -0.9326603190344698, -0.04908738521234052, 0.8881748761857863]}, time: 5}'" C-m

