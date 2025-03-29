#!/bin/zsh

# Initial delay of 10 seconds
sleep 10

# Create a new tmux session named "discovery" with an initial window named "cb"
tmux new-session -d -s discovery -n cb
tmux send-keys -t discovery:cb "cb; tmux wait-for -S cb_done" C-m

# Create a window "fastdds" that waits for cb to finish then runs fastdds discovery
tmux new-window -t discovery -n fastdds
tmux send-keys -t discovery:fastdds "tmux wait-for cb_done; fastdds discovery -i 0 -p 11811" C-m

# --- Window for Arm node ---
tmux new-window -t discovery -n arm
tmux send-keys -t discovery:arm "tmux wait-for cb_done; ros2 launch maurice_arm arm.launch.py" C-m

# --- Window for Control node ---
tmux new-window -t discovery -n control
tmux send-keys -t discovery:control "tmux wait-for cb_done; ros2 launch maurice_control app.launch.py" C-m

# --- Window for Bringup node ---
tmux new-window -t discovery -n bringup
tmux send-keys -t discovery:bringup "tmux wait-for cb_done; ros2 launch maurice_bringup bringup_core.launch.py" C-m

# --- Window for Camera node ---
tmux new-window -t discovery -n camera
tmux send-keys -t discovery:camera "tmux wait-for cb_done; ros2 launch maurice_bringup camera.launch.py" C-m

# --- Window for Recorder node ---
tmux new-window -t discovery -n recorder
tmux send-keys -t discovery:recorder "tmux wait-for cb_done; ros2 launch manipulation recorder.launch.py" C-m

# --- Window for Service Call ---
tmux new-window -t discovery -n service
sleep 10
tmux send-keys -t discovery:service "tmux wait-for cb_done; ros2 service call /maurice_arm/goto_js maurice_msgs/srv/GotoJS '{data: {data: [0.8528933180644165, -0.45712627478992107, 1.2946797849754812, -0.9326603190344698, -0.04908738521234052, 0.8881748761857863]}, time: 2}'" C-m

# Finally, attach to the tmux session so you can monitor all windows
tmux attach-session -t discovery
