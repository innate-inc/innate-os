#!/bin/bash
# PID 1 of a demo session container: bring up the world, then the ROS fleet,
# then hold the session open for its lease and exit.
#
# Unlike the dev sim there is no launcher and no host process -- the world
# server runs in this container (sim_driver.launch.py documents the
# VIRTUAL_MARS_REMOTE escape hatch). The dev sim keeps the world on the host
# because native GL renders ~7x faster than software GL; a headless cloud
# instance has no native GL either way, so that reason does not apply here.
set -euo pipefail

# Only what the world server needs -- it links no rclpy, so the Zenoh/RMW env
# (config/dds/setup_dds.zsh) belongs to the tmux shells, which get it from .zshrc.
# ROS's setup.bash reads AMENT_TRACE_SETUP_FILES while unset, which -u makes fatal.
set +u
source /opt/ros/humble/setup.bash
source /root/innate-os/ros2_ws/install/setup.bash
set -u

: "${INNATE_DEMO_LEASE_SECONDS:=600}"
: "${INNATE_SIM_RENDER_SCALE:=2}"
: "${MUJOCO_GL:=osmesa}"
export MUJOCO_GL INNATE_SIM_RENDER_SCALE

WORLD_LOG=/root/world-server.log

# The render scale is baked into the compiled model (it sets the texture cap),
# so overriding it here would miss the cache and put a multi-minute compile on
# the session-start path. Refuse rather than boot mysteriously slowly.
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

# The world must answer before the driver node connects, or sim_driver dies on
# its first RPC and the fleet comes up against a world that isn't there.
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

# The lease is enforced by the broker too; this is the backstop for a session
# the broker loses track of. Exiting PID 1 is what stops the billing.
if [ "$INNATE_DEMO_LEASE_SECONDS" -gt 0 ]; then
    echo "[demo] session lease: ${INNATE_DEMO_LEASE_SECONDS}s"
    sleep "$INNATE_DEMO_LEASE_SECONDS"
    echo "[demo] lease expired, shutting down"
else
    echo "[demo] no lease (INNATE_DEMO_LEASE_SECONDS=0), running until stopped"
    sleep infinity
fi
