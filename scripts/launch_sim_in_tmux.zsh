#!/bin/zsh

# launch-sim-in-tmux.zsh
# Launches simulation environment in organized tmux windows
# Usage: ./scripts/launch-sim-in-tmux.zsh [--detach] [--brain-websocket-uri URI] [--brain-client-version VERSION]

ATTACH=1
# Precedence: CLI flag > inherited env (compose bakes it) > derived from .env
# below. An argless in-container `innate restart` must keep the brain wiring
# rather than fall back to hosted -- and .env is the always-mounted source of
# truth, so deriving from it is robust even on containers created before the
# env was baked in.
BRAIN_WEBSOCKET_URI="${BRAIN_WEBSOCKET_URI:-}"
BRAIN_CLIENT_VERSION="${BRAIN_CLIENT_VERSION:-}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --detach)
      ATTACH=0
      shift
      ;;
    --brain-websocket-uri)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --brain-websocket-uri" >&2
        exit 2
      fi
      BRAIN_WEBSOCKET_URI="$2"
      shift 2
      ;;
    --brain-websocket-uri=*)
      BRAIN_WEBSOCKET_URI="${1#*=}"
      shift
      ;;
    --brain-client-version)
      if [[ $# -lt 2 ]]; then
        echo "Missing value for --brain-client-version" >&2
        exit 2
      fi
      BRAIN_CLIENT_VERSION="$2"
      shift 2
      ;;
    --brain-client-version=*)
      BRAIN_CLIENT_VERSION="${1#*=}"
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

# Derive the brain backend from the mounted .env when nothing set it (mirrors
# the launcher's resolve_brain_websocket_uri): a local Gemini brain -- key set,
# no hosted service key -- talks to the cloud-agent container; hosted/none
# leave it empty so brain_client uses its own default.
if [[ -z "$BRAIN_WEBSOCKET_URI" ]]; then
  _env_file="${INNATE_OS_ROOT:-$HOME/innate-os}/.env"
  if [[ -f "$_env_file" ]] \
    && grep -qE '^GEMINI_API_KEY=.+' "$_env_file" \
    && ! grep -qE '^INNATE_SERVICE_KEY=.+' "$_env_file"; then
    BRAIN_WEBSOCKET_URI="ws://cloud-agent:8765"
  fi
fi

SESSION_NAME="${INNATE_SIM_TMUX_SESSION:-innate}"
# Use braces in tmux targets so zsh does not interpret ":foo" as a parameter modifier.
TMUX_TARGET_PREFIX="${SESSION_NAME}"
STARTUP_SETTLE_SECONDS="${INNATE_SIM_TMUX_SETTLE_SECONDS:-0}"
TMUX_CLEANUP_SETTLE_SECONDS="${INNATE_SIM_TMUX_CLEANUP_SETTLE_SECONDS:-0}"

settle_after_launch() {
  if [[ "$STARTUP_SETTLE_SECONDS" != "0" && "$STARTUP_SETTLE_SECONDS" != "0.0" ]]; then
    sleep "$STARTUP_SETTLE_SECONDS"
  fi
}

# First, ensure we have a clean tmux environment
tmux kill-session -t "$SESSION_NAME" 2>/dev/null
sleep "$TMUX_CLEANUP_SETTLE_SECONDS"

# Create a new tmux session for the local Innate runtime
tmux new-session -d -x 240 -y 72 -s "$SESSION_NAME" -n zenoh

# === Window 0: Zenoh Router ===
tmux send-keys -t "${TMUX_TARGET_PREFIX}:zenoh" "ros2 run rmw_zenoh_cpp rmw_zenohd" C-m
echo "Started Zenoh router..."
settle_after_launch

# === Window 1: Rosbridge + App ===
tmux new-window -t "$SESSION_NAME" -n rosbridge-app
tmux send-keys -t "${TMUX_TARGET_PREFIX}:rosbridge-app" "ros2 launch mars_sim_bringup sim_rosbridge.launch.py" C-m
echo "Started rosbridge..."
settle_after_launch
# Split and run app
tmux split-window -t "${TMUX_TARGET_PREFIX}:rosbridge-app" -h
tmux send-keys -t "${TMUX_TARGET_PREFIX}:rosbridge-app.1" "ros2 launch mars_control app.sim.launch.py" C-m
echo "Started app control..."

# === Window 2: Virtual MARS driver (MuJoCo sim backend) ===
# Headless MuJoCo impersonating the hardware drivers at the ROS topic level
# (odom/TF, /scan, cameras, depth/points, arm/head) -- see
# ros2_ws/src/mars_bot/mars_sim_driver. Runs in-container, zenoh graph.
#
# NOTE on in-browser video: the C++ mars_cam WebRTC streamer stays disabled
# in the sim -- container-on-docker-bridge ICE candidates are unreachable
# from the browser (host candidates carry the container IP; the mDNS /
# srflx paths collapse at the docker NAT; host networking is unsupported on
# Docker Desktop). Camera viewing in sim goes through the webapp's
# SimSession (rosbridge state + local render) or Foxglove image panels.
tmux new-window -t "$SESSION_NAME" -n sim-driver
tmux send-keys -t "${TMUX_TARGET_PREFIX}:sim-driver" "ros2 launch mars_sim_driver sim_driver.launch.py" C-m
echo "Started virtual MARS driver (MuJoCo)..."
settle_after_launch

# === Window 3: Nav + Brain ===
# The REAL navigation stack (mode manager, router, namespaced planners, AMCL,
# velocity smoother) -- the sim substitutes only the CUDA grid_localizer (see
# mars_sim_driver) and seeds the maps dir with the generated apartment map so
# the mode manager boots straight into navigation mode.
mkdir -p ~/innate-os/data/maps
cp ~/innate-os/sim/assets/map/sim_apartment.* ~/innate-os/data/maps/ 2>/dev/null || true
tmux new-window -t "$SESSION_NAME" -n nav-brain
tmux send-keys -t "${TMUX_TARGET_PREFIX}:nav-brain" "ros2 launch mars_nav mode_manager.launch.py" C-m
echo "Started navigation system..."
settle_after_launch
# Split and run brain client
tmux split-window -t "${TMUX_TARGET_PREFIX}:nav-brain" -h
brain_client_cmd="ros2 launch brain_client brain_client.sim.launch.py"
if [[ -n "$BRAIN_WEBSOCKET_URI" ]]; then
  brain_websocket_arg="websocket_uri:=$BRAIN_WEBSOCKET_URI"
  brain_client_cmd+=" ${(q)brain_websocket_arg}"
fi
if [[ -n "$BRAIN_CLIENT_VERSION" ]]; then
  brain_client_version_arg="client_version:=$BRAIN_CLIENT_VERSION"
  brain_client_cmd+=" ${(q)brain_client_version_arg}"
fi
tmux send-keys -t "${TMUX_TARGET_PREFIX}:nav-brain.1" "$brain_client_cmd" C-m
echo "Started brain client..."

# === Window 4: Behavior Server ===
tmux new-window -t "$SESSION_NAME" -n behavior
tmux send-keys -t "${TMUX_TARGET_PREFIX}:behavior" "ros2 launch manipulation behavior.launch.py" C-m
echo "Started behavior server..."

# === Window 5: Arm IK ===
tmux new-window -t "$SESSION_NAME" -n arm-ik
tmux send-keys -t "${TMUX_TARGET_PREFIX}:arm-ik" "ros2 run mars_arm ik.py" C-m
echo "Started arm IK..."

# === Window 6: Vision Navigation Inference Client ===
tmux new-window -t "$SESSION_NAME" -n vision-nav
tmux send-keys -t "${TMUX_TARGET_PREFIX}:vision-nav" "ros2 launch innate_uninavid uninavid.launch.py cmd_vel_topic:=/cmd_vel" C-m
echo "Started vision navigation inference client..."
settle_after_launch

# === Window 7: Console + Webapp UI ===
# The robot webapp serves the sim UI too (replacing sim/frontend). Its python
# front door binds 443 (https) + 80 (http) inside the container — both exposed by
# docker-compose.dev.yml — and proxies /ws to the sim rosbridge on 9090.
tmux new-window -t "$SESSION_NAME" -n console-webapp
tmux send-keys -t "${TMUX_TARGET_PREFIX}:console-webapp" "ros2 launch innate_console console.launch.py" C-m
echo "Started console..."
settle_after_launch
tmux split-window -t "${TMUX_TARGET_PREFIX}:console-webapp" -h
# WEBAPP_SIM_CONTROLS makes the webapp pick the rendered SimSession for its
# camera panel (js/robotSession.js) instead of WebRTC.
tmux send-keys -t "${TMUX_TARGET_PREFIX}:console-webapp.1" "cd ~/innate-os/webapp && while true; do WEBAPP_SIM_CONTROLS=1 python3 proxy/https_server.py; sleep 2; done" C-m
echo "Started webapp (https :443 + http :80)..."

# === Window 8: Foxglove Bridge ===
# Always on in the sim: connect Foxglove Studio to ws://localhost:8765 (host
# side published by sim/docker-compose.dev.yml; SIM_FOXGLOVE_PORT/BIND
# override, and the launcher shifts it to 8766 when the local brain owns 8765).
tmux new-window -t "$SESSION_NAME" -n foxglove
tmux send-keys -t "${TMUX_TARGET_PREFIX}:foxglove" "ros2 launch foxglove_bridge foxglove_bridge_launch.xml" C-m
echo "Started Foxglove bridge (ws :8765)..."

# Select the rosbridge-app window
tmux select-window -t "${TMUX_TARGET_PREFIX}:rosbridge-app"

if [[ $ATTACH -eq 1 ]]; then
  echo "All services started in tmux session '$SESSION_NAME'. Attaching to session..."
  sleep 1
  tmux attach-session -t "$SESSION_NAME"
else
  echo "All services started in tmux session '$SESSION_NAME'."
  echo "Attach with: tmux attach-session -t $SESSION_NAME"
fi
