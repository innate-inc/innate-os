#!/bin/bash
# PID 1 of a session: world server, then the ROS fleet, then hold for the lease.
set -euo pipefail

# ROS's setup.bash reads AMENT_TRACE_SETUP_FILES while unset, which -u makes fatal.
set +u
source /opt/ros/humble/setup.bash
source /root/innate-os/ros2_ws/install/setup.bash
set -u

# Fail closed before starting any visitor-reachable process. Credentials belong
# only in the separately isolated cloud relay, never this container.
python3 -m innate_proxy.public_demo

: "${INNATE_DEMO_LEASE_SECONDS:=600}"
: "${INNATE_SIM_RENDER_SCALE:=2}"
: "${MUJOCO_GL:=osmesa}"
export MUJOCO_GL INNATE_SIM_RENDER_SCALE

WORLD_LOG=/root/world-server.log

# The scale is baked into the compiled model; a different one misses the cache
# and puts a multi-minute compile on the session-start path. Refuse instead.
BAKED_SCALE_FILE="$VIRTUAL_MARS_ASSETS/.model_cache/render_scale"
if [ -f "$BAKED_SCALE_FILE" ]; then
    BAKED_SCALE=$(cat "$BAKED_SCALE_FILE")
    if [ "$BAKED_SCALE" != "$INNATE_SIM_RENDER_SCALE" ]; then
        echo "[demo] INNATE_SIM_RENDER_SCALE=$INNATE_SIM_RENDER_SCALE but the model cache was" \
             "baked at $BAKED_SCALE -- rebuild the image with that scale instead." >&2
        exit 1
    fi
fi

shutdown() {
    tmux kill-server >/dev/null 2>&1 || true
    kill "${WORLD_PID:-}" >/dev/null 2>&1 || true
}
trap shutdown EXIT

echo "[demo] starting world server (MUJOCO_GL=$MUJOCO_GL, render-scale=$INNATE_SIM_RENDER_SCALE)"
ros2 run mars_sim_driver world_server \
    --bind 127.0.0.1 --port 8799 --state-port 8800 \
    --render-scale "$INNATE_SIM_RENDER_SCALE" >"$WORLD_LOG" 2>&1 &
WORLD_PID=$!

# sim_driver dies on its first RPC if the world is not up yet.
for _ in $(seq 1 120); do
    if grep -q "GL self-test" "$WORLD_LOG" 2>/dev/null; then break; fi
    if ! kill -0 "$WORLD_PID" 2>/dev/null; then
        echo "[demo] world server died on boot:" >&2
        cat "$WORLD_LOG" >&2
        exit 1
    fi
    sleep 0.5
done
grep "GL self-test" "$WORLD_LOG" || { echo "[demo] world server never reported GL readiness" >&2; exit 1; }

echo "[demo] starting ROS fleet"
/root/innate-os/sim/demo/launch_demo.zsh

# Backstop for a session the broker loses track of: exiting PID 1 stops the bill.
if [ "$INNATE_DEMO_LEASE_SECONDS" -gt 0 ]; then
    echo "[demo] session lease: ${INNATE_DEMO_LEASE_SECONDS}s"
    sleep "$INNATE_DEMO_LEASE_SECONDS"
    echo "[demo] lease expired, shutting down"
else
    echo "[demo] no lease (INNATE_DEMO_LEASE_SECONDS=0), running until stopped"
    sleep infinity
fi
