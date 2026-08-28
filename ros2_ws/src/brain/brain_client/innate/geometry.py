#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Head-camera <-> floor (z=0) via pinhole + URDF.

Import as ``from innate import geometry`` (moved from workspace/skill_lib)."""

import math

HEAD_ORIGIN = (-0.040751, -0.0002, 0.25882)  # base_link -> head joint (URDF)
CAM_IN_HEAD = (0.04327, 0.0297, -0.000275)  # head -> left camera optical
IMG_W, IMG_H = 640, 480

# Factory-sample intrinsics of the left eye (1280x720: fx~=fy~=400.8,
# cx=638.2, cy=373.1) scaled through the driver's NON-UNIFORM resize to the
# published 640x480 (x0.5 horizontal, x2/3 vertical), so FX != FY. The old
# single-FX model assumed a 70 deg HFOV lens; the real lens is ~116 deg, and
# that error read floor ranges ~2x long (tape-measured 2026-08-28: a point
# at 0.156 m reported as 0.33). Lenses vary a few percent per robot — verify
# by laying an object on a drawn park marker and taping the distance; tune
# FY first, it dominates range. Distortion is not modelled.
FX, FY = 200.3, 267.3
CX, CY = 319.1, 248.7


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
