# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Camera wire constants shared by the ROS adapter (node.py) and the world
side (core.py / world_server.py). Kept MuJoCo-free on purpose: the adapter
runs in the OS container, which does not ship MuJoCo -- the world itself
always runs on the host (see world_server.py)."""

CAMERA_WIDTH, CAMERA_HEIGHT = 640, 480
CAMERA_FOVY = 80  # vertical FOV; keep in sync with sim/viewer's ROBOT_CAMERA_VFOV
