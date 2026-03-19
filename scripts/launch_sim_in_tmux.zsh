#!/bin/zsh

# launch-sim-in-tmux.zsh
# Launches simulation environment in organized tmux windows
# Usage: ./scripts/launch-sim-in-tmux.zsh [--detach]

ATTACH=1
if [[ "$1" == "--detach" ]]; then
  ATTACH=0
fi

# First, ensure we have a clean tmux environment
tmux kill-session -t mars 2>/dev/null

# Create a new tmux session named 'mars'
tmux new-session -d -x 240 -y 72 -s mars -n zenoh

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
tmux split-window -t mars:rosbridge-app -h
tmux send-keys -t mars:rosbridge-app.1 "ros2 launch maurice_control app.sim.launch.py" C-m
echo "Started app control..."
# === Window 2: WebRTC Streamer ===
tmux new-window -t mars -n webrtc
tmux send-keys -t mars:webrtc "ros2 launch maurice_cam webrtc_streamer.sim.launch.py" C-m
echo "Started webrtc streamer (sim mode with compressed images)..."

# === Window 3: Nav + Brain ===
tmux new-window -t mars -n nav-brain
tmux send-keys -t mars:nav-brain "ros2 launch maurice_nav navigation_sim.launch.py" C-m
echo "Started navigation system..."
sleep 2
# Split and run brain client
tmux split-window -t mars:nav-brain -h
tmux send-keys -t mars:nav-brain.1 "ros2 launch brain_client brain_client.sim.launch.py" C-m
echo "Started brain client..."

# === Window 4: Behavior Server ===
tmux new-window -t mars -n behavior
tmux send-keys -t mars:behavior "ros2 launch manipulation behavior.launch.py" C-m
echo "Started behavior server..."

# Select the rosbridge-app window
tmux select-window -t mars:rosbridge-app

if [[ $ATTACH -eq 1 ]]; then
  echo "All services started in tmux session 'mars'. Attaching to session..."
  sleep 1
  tmux attach-session -t mars
else
  echo "All services started in tmux session 'mars'."
  echo "Attach with: tmux attach-session -t mars"
fi
