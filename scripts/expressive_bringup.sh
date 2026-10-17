#!/bin/zsh
# Staged hardware bring-up of the expression layer, run on the robot with the agent stopped:
#   scripts/expressive_bringup.sh check | head | voice | arm [--rounds N] | full
# Protocol and acceptance thresholds: docs/EXPRESSIVE.md, "Hardware bring-up". Logs: /tmp/expressive_bringup/
set -e
ROOT=${INNATE_OS_ROOT:-${0:A:h:h}}
if ! python3 -c "import rclpy, brain_client" 2>/dev/null; then
    source "$ROOT/config/dds/setup_dds.zsh"
    source "$ROOT/ros2_ws/install/setup.zsh"
fi
exec python3 "$ROOT/scripts/expressive_bringup.py" "$@"
