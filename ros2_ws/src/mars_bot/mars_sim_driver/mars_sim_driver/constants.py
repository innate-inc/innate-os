# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Camera wire constants shared by the ROS adapter (node.py) and the world
side (core.py / world_server.py). Kept MuJoCo-free on purpose: the adapter
runs in the OS container, which does not ship MuJoCo -- the world itself
always runs on the host (see world_server.py)."""

import math

# Wire resolution: what every consumer sees.
CAMERA_WIDTH, CAMERA_HEIGHT = 640, 480

# The REAL head camera, measured off a calibrated robot (2026-08-28): a ~116
# degree lens whose left eye calibrates to fx = fy = 400.8 at its native
# 1280x720, which the driver then resizes NON-UNIFORMLY to 640x480 — which is
# what makes the published frame anisotropic, and why the principal point is
# not the frame centre. innate.geometry models exactly these numbers, so the
# sim renders this lens rather than one of its own: with a 68.5 degree
# square-pixel camera the skills read lateral offsets 1.6x large and the
# centring servo over-turned and oscillated instead of converging.
#
# MuJoCo takes them directly (MjSpec camera resolution/focal_pixel/
# principal_pixel), so there is no resize step and no aspect trick. Distortion
# is modelled in neither place.
CAMERA_FX, CAMERA_FY = 200.3, 267.3
CAMERA_CX, CAMERA_CY = 319.1, 248.7
# MuJoCo's principal_pixel is the SENSOR shift, so it negates on both axes
# relative to an image-space (cx, cy) — verified by rendering markers at known
# floor positions (2026-08-28: mean 0.5 px against geometry.floor_to_pixel).
CAMERA_PRINCIPAL_PIXEL = (CAMERA_WIDTH / 2 - CAMERA_CX, CAMERA_HEIGHT / 2 - CAMERA_CY)
# Kept for the viewer's preview camera, which has square pixels and so can
# only match one axis; the vertical is the honest one.
CAMERA_FOVY = math.degrees(2 * math.atan(CAMERA_HEIGHT / (2 * CAMERA_FY)))  # ~83.9

# The wrist camera is a different, uncalibrated lens; the skill's wrist-servo
# constants are tuned on it and converge in sim at 80, so it keeps 80 until
# someone measures the real module.
WRIST_CAMERA_FOVY = 80
