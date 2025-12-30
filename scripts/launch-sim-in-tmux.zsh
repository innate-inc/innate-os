#!/bin/zsh

# launch-sim-in-tmux.zsh
# Launches simulation environment in organized tmux windows
# Usage: ./scripts/launch-sim-in-tmux.zsh

# First, ensure we have a clean tmux environment
tmux kill-session -t mars 2>/dev/null

# Create a new tmux session named 'mars'
tmux new-session -d -s mars -n zenoh

# === Window 0: Zenoh Router ===
tmux send-keys -t mars:zenoh "ros2 run rmw_zenoh_cpp rmw_zenohd" C-m
echo "Started Zenoh router..."
sleep 2  # Give Zenoh router time to start up

# === Window 1: Rosbridge + App ===
tmux new-window -t mars -n rosbridge-app
tmux send-keys -t mars:rosbridge-app "ros2 launch maurice_sim_bringup sim_rosbridge.launch.py" C-m
echo "Started rosbridge..."
sleep 2
# Split and run app
tmux split-window -t mars:rosbridge-app -v
tmux send-keys -t mars:rosbridge-app.1 "ros2 launch maurice_control app.sim.launch.py" C-m
echo "Started app control..."

# === Window 2: Nav + Brain ===
tmux new-window -t mars -n nav-brain
tmux send-keys -t mars:nav-brain "ros2 launch maurice_nav navigation_sim.launch.py" C-m
echo "Started navigation system..."
sleep 2
# Split and run brain client
tmux split-window -t mars:nav-brain -v
tmux send-keys -t mars:nav-brain.1 "ros2 launch brain_client brain_client.sim.launch.py" C-m
echo "Started brain client..."

# Select the rosbridge-app window
tmux select-window -t mars:rosbridge-app

# Attach to the tmux session
echo "All services started in tmux session 'mars'. Attaching to session..."
sleep 1
tmux attach-session -t mars