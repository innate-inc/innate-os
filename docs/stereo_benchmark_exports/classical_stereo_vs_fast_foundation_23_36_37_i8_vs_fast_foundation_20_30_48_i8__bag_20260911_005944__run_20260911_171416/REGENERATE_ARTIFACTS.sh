#!/usr/bin/env bash
set -euo pipefail

ROOT="/home/jetson1/innate-os"
RUN_DIR="/home/jetson1/innate-os/recordings/stereo_benchmark_runs/classical_stereo_vs_fast_foundation_23_36_37_i8_vs_fast_foundation_20_30_48_i8__bag_20260911_005944__run_20260911_171416"

source /opt/ros/humble/setup.zsh
source "$ROOT/ros2_ws/install/setup.zsh"
python3 "$ROOT/scripts/stereo_benchmark_postprocess.py" \
  --run-dir "$RUN_DIR" \
  --columns 3
