# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Camera wire constants shared by the ROS adapter (node.py) and the world
side (core.py / world_server.py). Kept MuJoCo-free on purpose: the adapter
runs in the OS container, which does not ship MuJoCo -- the world itself
always runs on the host (see world_server.py)."""

import math

# Wire resolution: what every consumer sees.
CAMERA_WIDTH, CAMERA_HEIGHT = 640, 480

# The REAL head camera (calibrated 2026-08-28): a ~116 deg lens resized
# non-uniformly to 640x480, hence fx != fy and an off-centre principal point.
# innate.geometry models exactly these; rendering any other lens miscalibrates
# every skill.
CAMERA_FX, CAMERA_FY = 200.3, 267.3
CAMERA_CX, CAMERA_CY = 319.1, 248.7
# MuJoCo's principal_pixel is the SENSOR shift: negated on both axes against
# an image-space (cx, cy). Determined by rendering, not assumed.
CAMERA_PRINCIPAL_PIXEL = (CAMERA_WIDTH / 2 - CAMERA_CX, CAMERA_HEIGHT / 2 - CAMERA_CY)
# Kept for the viewer's preview camera, which has square pixels and so can
# only match one axis; the vertical is the honest one.
CAMERA_FOVY = math.degrees(2 * math.atan(CAMERA_HEIGHT / (2 * CAMERA_FY)))  # ~83.9

# The wrist camera is a different, uncalibrated lens; the skill's wrist-servo
# constants are tuned on it and converge in sim at 80, so it keeps 80 until
# someone measures the real module.
WRIST_CAMERA_FOVY = 80

# The nav policy only ever saw the mars preset's front_rect view: a 640x480
# pinhole at 110 deg HORIZONTAL fov. main is 84.5 deg horizontal -- a 1.57x
# longer focal length, which the policy reads as floor distances 36% nearer.
WIDE_CAMERA_HFOV = 110.0
WIDE_CAMERA_FOVY = math.degrees(
    2 * math.atan(math.tan(math.radians(WIDE_CAMERA_HFOV / 2)) * CAMERA_HEIGHT / CAMERA_WIDTH)
)
