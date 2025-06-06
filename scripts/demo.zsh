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
tmux send-keys -t discovery:bringup "ros2 launch maurice_bringup maurice_bringup.launch.py" C-m

# --- Window for Recorder node ---
tmux new-window -t discovery -n recorder
tmux send-keys -t discovery:recorder "ros2 launch manipulation recorder.launch.py" C-m

# --- Window for navigation ---
sleep 5
tmux new-window -t discovery -n navigation
tmux send-keys -t discovery:navigation "ros2 launch maurice_nav navigation.launch.py" C-m

# --- Window for Service Call ---
sleep 5
tmux new-window -t discovery -n service
tmux send-keys -t discovery:service "ros2 service call /maurice_arm/goto_js maurice_msgs/srv/GotoJS '{data: {data: [0.8528933180644165, -0.45712627478992107, 1.2946797849754812, -0.9326603190344698, -0.04908738521234052, 0.8881748761857863]}, time: 5}'" C-m

# --- Window for brain ---
sleep 5
tmux new-window -t discovery -n brain
tmux send-keys -t discovery:brain_client "ros2 launch brain_client brain_client.launch.py" C-m

# --- Window for policy ---
sleep 5
tmux new-window -t discovery -n policy
tmux send-keys -t discovery:policy "ros2 run manipulation behavior.py" C-m

# --- Window for policy ---
