#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Head-camera <-> floor (z=0) via pinhole + URDF.

Import as ``from innate import geometry`` (moved from workspace/skill_lib)."""

import math

HEAD_ORIGIN = (-0.040751, -0.0002, 0.25882)  # base_link -> head joint (URDF)
CAM_IN_HEAD = (0.04327, 0.0297, -0.000275)  # head -> left camera optical
ARM_ORIGIN = (0.086, -0.05285, 0.04025)  # base_link -> joint1 axis (URDF)
IMG_W, IMG_H = 640, 480

# Left-eye factory intrinsics (1280x720, fx~=fy~=400.8) through the driver's
# NON-UNIFORM resize to 640x480, so FX != FY. Tape-measured 2026-08-28: the old
# 70 deg model read 0.156 m as 0.33. Tune FY first — it dominates range.
FX, FY = 200.3, 267.3
CX, CY = 319.1, 248.7


def arm_bearing(x, y):
    """Heading from the arm's own base to a base_link point. The whole arm
    lies in the vertical plane through joint1's axis, so this IS the yaw a
    grasp there must use; from the base_link origin it is ~18 deg off at
    grasp range, which a vertical tool can only absorb in the wrist roll."""
    return math.atan2(y - ARM_ORIGIN[1], x - ARM_ORIGIN[0])


def _head_rot(tilt_rad):
    c, s = math.cos(tilt_rad), math.sin(tilt_rad)
    return ((c, 0.0, -s), (0.0, 1.0, 0.0), (s, 0.0, c))


def _rot(R, v):
    return tuple(sum(R[i][k] * v[k] for k in range(3)) for i in range(3))


def _cam_pose(head_tilt_deg):
    """Camera origin + (fwd, right, down) axes in base_link for a head tilt."""
    R = _head_rot(math.radians(head_tilt_deg))
    off = _rot(R, CAM_IN_HEAD)
    cam = tuple(HEAD_ORIGIN[i] + off[i] for i in range(3))
    return cam, _rot(R, (1, 0, 0)), _rot(R, (0, -1, 0)), _rot(R, (0, 0, -1))


def pixel_to_floor(u, v, head_tilt_deg):
    """Pixel (u,v) -> floor (x,y) in base_link, or None."""
    cam, fwd, right, down = _cam_pose(head_tilt_deg)
    xo, yo = (u - CX) / FX, (v - CY) / FY
    d = tuple(fwd[i] + xo * right[i] + yo * down[i] for i in range(3))
    if d[2] >= -1e-6:
        return None
    t = -cam[2] / d[2]
    x, y = cam[0] + t * d[0], cam[1] + t * d[1]
    return (x, y) if x > 0 else None


def floor_to_pixel(x, y, head_tilt_deg):
    """Floor (x,y) -> pixel, or None. Inverse of pixel_to_floor."""
    cam, fwd, right, down = _cam_pose(head_tilt_deg)
    D = (x - cam[0], y - cam[1], -cam[2])
    a = sum(D[i] * fwd[i] for i in range(3))
    if a <= 1e-6:
        return None
    b = sum(D[i] * right[i] for i in range(3))
    c = sum(D[i] * down[i] for i in range(3))
    return (CX + (b / a) * FX, CY + (c / a) * FY)


def pixel_to_height(u, v, head_tilt_deg, x):
    """Height (base_link z) where pixel (u,v)'s ray crosses the vertical line
    at forward distance ``x``, or None. Reads the rim height of a box whose
    floor-contact edge has already been localized to ``x``."""
    cam, fwd, right, down = _cam_pose(head_tilt_deg)
    xo, yo = (u - CX) / FX, (v - CY) / FY
    d = tuple(fwd[i] + xo * right[i] + yo * down[i] for i in range(3))
    if abs(d[0]) < 1e-6:
        return None
    t = (x - cam[0]) / d[0]
    return cam[2] + t * d[2] if t > 0 else None
