#!/bin/zsh -l
#
# The demo stack: scripts/launch_sim_in_tmux.zsh minus everything a browser
# visitor cannot reach. Kept as its own file rather than flags on the dev
# launcher so a change to the dev sim can never silently widen the demo.
#
# Dropped vs the dev sim:
#   foxglove_bridge        -- ws:8765 is never published to a demo visitor
#   udp_leader_receiver    -- leader-arm teleop needs hardware on the operator side
#   innate_console         -- the terminal console is not exposed
# Kept: rosbridge, app, sim driver, nav, brain, behavior, input manager, arm IK,
# uninavid (the VLN demo), webapp.

set -e

SESSION_NAME="${INNATE_SIM_TMUX_SESSION:-innate}"

tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

tmux new-session -d -x 240 -y 72 -s "$SESSION_NAME" -n zenoh
tmux send-keys -t "${SESSION_NAME}:zenoh" "ros2 run rmw_zenoh_cpp rmw_zenohd" C-m

tmux new-window -t "$SESSION_NAME" -n rosbridge-app
tmux send-keys -t "${SESSION_NAME}:rosbridge-app" "ros2 launch mars_sim_bringup sim_rosbridge.launch.py" C-m
tmux split-window -t "${SESSION_NAME}:rosbridge-app" -h
tmux send-keys -t "${SESSION_NAME}:rosbridge-app.1" "ros2 launch mars_control app.sim.launch.py" C-m

tmux new-window -t "$SESSION_NAME" -n sim-driver
tmux send-keys -t "${SESSION_NAME}:sim-driver" "ros2 launch mars_sim_driver sim_driver.launch.py" C-m

mkdir -p ~/innate-os/data/maps
cp ~/innate-os/sim/assets/map/sim_apartment.* ~/innate-os/data/maps/ 2>/dev/null || true

tmux new-window -t "$SESSION_NAME" -n nav-brain
tmux send-keys -t "${SESSION_NAME}:nav-brain" "ros2 launch mars_nav mode_manager.launch.py" C-m
tmux split-window -t "${SESSION_NAME}:nav-brain" -h
tmux send-keys -t "${SESSION_NAME}:nav-brain.1" "ros2 launch brain_client brain_client.sim.launch.py" C-m

tmux new-window -t "$SESSION_NAME" -n behavior
tmux send-keys -t "${SESSION_NAME}:behavior" "ros2 launch manipulation behavior.launch.py" C-m
tmux split-window -t "${SESSION_NAME}:behavior" -h
tmux send-keys -t "${SESSION_NAME}:behavior.1" "ros2 launch brain_client input_manager.launch.py" C-m

tmux new-window -t "$SESSION_NAME" -n arm-ik
tmux send-keys -t "${SESSION_NAME}:arm-ik" "ros2 run mars_arm ik.py" C-m

if [[ "${INNATE_DEMO_UNINAVID:-1}" == "1" ]]; then
  tmux new-window -t "$SESSION_NAME" -n vision-nav
  tmux send-keys -t "${SESSION_NAME}:vision-nav" "ros2 launch innate_uninavid uninavid.launch.py cmd_vel_topic:=/cmd_vel" C-m
fi

# Only :80 is ever routed to a visitor -- the demo sits behind a TLS-terminating
# proxy, so the self-signed cert on :443 (and the warning it costs) never shows.
tmux new-window -t "$SESSION_NAME" -n webapp
tmux send-keys -t "${SESSION_NAME}:webapp" \
  "cd ~/innate-os/webapp && while true; do WEBAPP_SIM_CONTROLS=1 INNATE_WEBAPP_READONLY=1 python3 proxy/https_server.py; sleep 2; done" C-m

echo "demo stack started in tmux session '$SESSION_NAME'."
