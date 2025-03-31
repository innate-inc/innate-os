#!/bin/zsh

# discovery-and-launch-sim.zsh
# Place this file in the dds directory next to discovery.zsh

# First, ensure we have a clean tmux environment
tmux kill-session -t maurice 2>/dev/null

# Create a new tmux session named 'maurice'
tmux new-session -d -s maurice

# Run the discovery service in the first pane
tmux send-keys -t maurice:0.0 "fastdds discovery -i 0 -p 11811" C-m
echo "Started discovery service..."
sleep 2  # Give discovery time to start up

# Create a new pane and run the simulation
tmux split-window -t maurice:0.0 -v
tmux send-keys -t maurice:0.1 "ros2 launch maurice_sim_bringup sim_rosbridge.launch.py" C-m
echo "Started simulation..."
sleep 2  # Give rosbridge time to start up

# Create a new pane and run the nav system
tmux split-window -t maurice:0.1 -h
tmux send-keys -t maurice:0.2 "ros2 launch maurice_nav navigation_sim.launch.py" C-m
echo "Started navigation system..."
sleep 2  # Give nav system time to start up

# Create a new pane for the brain client
tmux split-window -t maurice:0.0 -h
# Let's add a debug message to see which pane we're currently in
tmux send-keys -t maurice:0.1 "ros2 launch brain_client brain_client.launch.py" C-m
echo "Started brain client..."

# Select the first pane
tmux select-pane -t maurice:0.1

# Attach to the tmux session
echo "All services started in tmux session 'maurice'. Attaching to session..."
sleep 1
tmux attach-session -t maurice