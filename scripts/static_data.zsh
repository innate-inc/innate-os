#!/bin/zsh
# This script creates a tmux session "static_data" and runs initial setup commands
# in separate windows. The "init" window runs the cb alias, and once it completes,
# it signals that the discovery window can proceed. The remaining windows launch
# long-running ROS2 nodes concurrently.

# Create a new tmux session with an initial window named "init"
tmux new-session -d -s static_data -n init
tmux send-keys -t static_data:0 "cb; tmux wait-for -S cb_done" C-m

# Create a new window "discovery" that waits for cb to finish, then runs fastdds discovery
tmux new-window -t static_data -n discovery
tmux send-keys -t static_data:1 "tmux wait-for cb_done; fastdds discovery -i 0 -p 11811" C-m

# Create additional windows for the long-running ROS2 nodes
sleep 5
# Window "arm": Launch the maurice_arm node
tmux new-window -t static_data -n arm
tmux send-keys -t static_data:2 "ros2 launch maurice_arm arm.launch.py" C-m

# Window "bringup": Launch the maurice_bringup node
tmux new-window -t static_data -n bringup
tmux send-keys -t static_data:3 "ros2 launch maurice_bringup camera.launch.py" C-m

# Window "control": Launch the maurice_control node
tmux new-window -t static_data -n control
tmux send-keys -t static_data:4 "ros2 launch maurice_control leader.launch.py" C-m

# Window "recorder": Launch the manipulation recorder node
tmux new-window -t static_data -n recorder
tmux send-keys -t static_data:5 "ros2 launch manipulation recorder.launch.py" C-m

# Finally, attach to the tmux session so you can monitor all windows
tmux attach-session -t static_data
