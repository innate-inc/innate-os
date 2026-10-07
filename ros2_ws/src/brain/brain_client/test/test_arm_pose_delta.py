# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The /armsdk/stream_pose contract shared with the controller app: the delta
is in the arm's heading frame (j1 at anchoring) and rotates about those axes
(delta ⊗ anchor), whatever the anchor's pitch."""

import math

import pytest

from brain_client.common.geometry import apply_pose_delta, unyaw_delta


def _about(axis, angle):
    s = math.sin(angle / 2)
    return (axis[0] * s, axis[1] * s, axis[2] * s, math.cos(angle / 2))


def test_delta_rotates_about_base_axes_and_translation_adds():
    anchor_xyz, anchor_q = (0.3, 0.0, 0.2), _about((0, 1, 0), 0.5)  # pitched down 0.5
    x, y, z, roll, pitch, yaw = apply_pose_delta(anchor_xyz, anchor_q, (0.05, -0.02, 0.01), _about((0, 0, 1), 0.3))
    assert (x, y, z) == pytest.approx((0.35, -0.02, 0.21))
    assert (roll, pitch, yaw) == pytest.approx((0.0, 0.5, 0.3))


def test_forward_follows_the_arm_heading_not_base_x():
    delta = unyaw_delta((0.05, 0.0, 0.0), _about((0, 1, 0), 0.3), math.pi / 2)  # arm turned left
    x, y, z, roll, pitch, yaw = apply_pose_delta((0.0, 0.2, 0.2), (0.0, 0.0, 0.0, 1.0), *delta)
    assert (x, y, z) == pytest.approx((0.0, 0.25, 0.2))
    assert (roll, pitch, yaw) == pytest.approx((-0.3, 0.0, 0.0))
