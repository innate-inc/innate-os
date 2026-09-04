# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Pins the /armsdk/stream_pose contract shared with the controller app: the
delta's rotation is applied about the base axes (delta ⊗ anchor), so a phone
turned left yaws the gripper left whatever the anchor's pitch was."""

import math

import pytest

from brain_client.common.geometry import apply_pose_delta


def _about(axis, angle):
    s = math.sin(angle / 2)
    return (axis[0] * s, axis[1] * s, axis[2] * s, math.cos(angle / 2))


def test_delta_rotates_about_base_axes_and_translation_adds():
    anchor_xyz, anchor_q = (0.3, 0.0, 0.2), _about((0, 1, 0), 0.5)  # pitched down 0.5
    x, y, z, roll, pitch, yaw = apply_pose_delta(anchor_xyz, anchor_q, (0.05, -0.02, 0.01), _about((0, 0, 1), 0.3))
    assert (x, y, z) == pytest.approx((0.35, -0.02, 0.21))
    assert (roll, pitch, yaw) == pytest.approx((0.0, 0.5, 0.3))
