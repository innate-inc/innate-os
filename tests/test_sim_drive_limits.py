# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Regression checks for the virtual MARS drive envelope."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PACKAGE = REPO_ROOT / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"
sys.path.insert(0, str(DRIVER_PACKAGE))

from mars_sim_driver.drive_limits import MAX_LINEAR, MAX_YAW, clamp_cmd_vel  # noqa: E402


def test_sim_accepts_mad_mars_top_speed() -> None:
    assert MAX_LINEAR == 0.8
    assert MAX_YAW == 2.0
    assert clamp_cmd_vel(0.8, 2.0) == (0.8, 2.0)


def test_sim_clamps_commands_beyond_mad_mars() -> None:
    assert clamp_cmd_vel(10.0, -10.0) == (MAX_LINEAR, -MAX_YAW)
