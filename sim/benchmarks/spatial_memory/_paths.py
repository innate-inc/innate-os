"""Puts mars_sim_driver (ROS package dir) and brain_client on sys.path so the
benchmark runs the production memory/retrieval code without a ROS install."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SIM_DIR = REPO_ROOT / "sim"

for pkg_dir in (
    REPO_ROOT / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver",
    REPO_ROOT / "ros2_ws" / "src" / "brain" / "brain_client",
):
    if str(pkg_dir) not in sys.path:
        sys.path.insert(0, str(pkg_dir))
