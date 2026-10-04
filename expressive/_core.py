"""Puts brain_client (the pure expressive core) and the mars_sim_driver package dirs on sys.path, so the
tools import `brain_client.expressive` and `mars_sim_driver.core` from this checkout without a ROS install.
The one sys.path shim in this project, mirroring sim/sandbox/_driver_pkg.py."""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "ros2_ws" / "src"
# innate-llm: brain_client.transport (the robot's sentence splitter) imports it
_PACKAGES = ("brain/brain_client", "mars_bot/mars_sim_driver", "cloud/clients/innate-llm")
for _pkg in (_SRC / p for p in _PACKAGES):
    if str(_pkg) not in sys.path:
        sys.path.insert(0, str(_pkg))
