"""Robot-side plan tracking: re-anchoring, pure pursuit, blocked detection.

Pure geometry -- no ROS, no network, no policy server. These are the pieces
that are silently wrong when they are wrong: a re-anchoring sign error degrades
every tracked path without ever raising.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from innate_nav.policy_client import Pose
from innate_nav.pursuit import BlockedDetector, PursuitCfg, reanchor, recovery_turn, steering_target, velocity

# --- re-anchoring ------------------------------------------------------------

def test_reanchor_is_identity_when_the_robot_has_not_moved():
    wp = np.array([[1.0, 0.5, 0.2], [2.0, -0.3, -0.1]])
    p = Pose(3.0, -2.0, 0.7)
    assert np.allclose(reanchor(wp, p, p), wp)


def test_reanchor_pure_translation_subtracts_travelled_distance():
    wp = np.array([[2.0, 0.0, 0.0]])
    out = reanchor(wp, Pose(0, 0, 0), Pose(0.5, 0, 0))   # drove 0.5 m forward
    assert out[0, 0] == pytest.approx(1.5)
    assert out[0, 1] == pytest.approx(0.0)


def test_reanchor_pure_rotation_rotates_the_target_the_other_way():
    wp = np.array([[1.0, 0.0, 0.0]])
    out = reanchor(wp, Pose(0, 0, 0), Pose(0, 0, math.pi / 2))  # turned 90 deg left
    assert out[0, 0] == pytest.approx(0.0, abs=1e-9)
    assert out[0, 1] == pytest.approx(-1.0)          # target is now to the right
    assert out[0, 2] == pytest.approx(-math.pi / 2)


def test_reanchor_matches_an_explicit_world_frame_round_trip():
    rng = np.random.default_rng(0)
    wp = rng.normal(size=(8, 3))
    cap, now = Pose(1.2, -0.4, 0.9), Pose(1.8, 0.1, 1.4)
    got = reanchor(wp, cap, now)
    for k in range(len(wp)):
        # capture frame -> world
        wx = cap.x + math.cos(cap.yaw) * wp[k, 0] - math.sin(cap.yaw) * wp[k, 1]
        wy = cap.y + math.sin(cap.yaw) * wp[k, 0] + math.cos(cap.yaw) * wp[k, 1]
        # world -> current frame
        dx, dy = wx - now.x, wy - now.y
        ex = math.cos(now.yaw) * dx + math.sin(now.yaw) * dy
        ey = -math.sin(now.yaw) * dx + math.cos(now.yaw) * dy
        assert got[k, 0] == pytest.approx(ex)
        assert got[k, 1] == pytest.approx(ey)


# --- pure pursuit ------------------------------------------------------------

def _straight_path(n=8, step=0.25):
    return np.array([[step * (i + 1), 0.0, 0.0] for i in range(n)])


def test_steering_target_uses_cumulative_arc_not_euclidean_distance():
    # a path that doubles back: cumulative length passes 0.75 before the
    # straight-line distance from the origin does
    wp = np.array([[0.3, 0.0, 0.0], [0.6, 0.0, 0.0], [0.6, 0.4, 0.0], [0.3, 0.6, 0.0]])
    tgt = steering_target(wp, 0.75)
    assert tgt.tolist() == [0.6, 0.4, 0.0]


def test_velocity_drives_forward_on_a_straight_path():
    v, w = velocity(_straight_path())
    assert v == pytest.approx(PursuitCfg().v_max)
    assert w == pytest.approx(0.0)


def test_velocity_turns_in_place_when_the_target_is_off_bearing():
    wp = np.array([[0.1, 0.4, 0.0], [0.2, 0.9, 0.0]])   # hard left
    v, w = velocity(wp)
    assert v == 0.0
    assert w > 0.0                                       # +ve = CCW = left


def test_velocity_slows_with_bearing_error_below_the_turn_threshold():
    straight_v, _ = velocity(_straight_path())
    angled = np.array([[0.7, 0.2, 0.0], [1.4, 0.4, 0.0]])   # ~16 deg, under 22.5
    angled_v, angled_w = velocity(angled)
    assert 0.0 < angled_v < straight_v
    assert angled_w > 0.0


def test_recovery_turn_spins_toward_the_furthest_reaching_waypoint():
    right = np.array([[0.2, -0.1, 0.0], [0.5, -1.2, 0.0]])
    assert recovery_turn(right) < 0.0
    left = np.array([[0.2, 0.1, 0.0], [0.5, 1.2, 0.0]])
    assert recovery_turn(left) > 0.0


# --- blocked detection -------------------------------------------------------

def test_blocked_detector_fires_when_commanded_motion_produces_none():
    det = BlockedDetector()
    stuck = Pose(0.0, 0.0, 0.0)
    blocked = False
    for i in range(20):
        blocked = det.update(i * 0.1, 0.4, stuck)
    assert blocked


def test_blocked_detector_stays_quiet_while_the_robot_actually_moves():
    det = BlockedDetector()
    for i in range(20):
        t = i * 0.1
        assert not det.update(t, 0.4, Pose(0.4 * t, 0.0, 0.0))


def test_blocked_detector_stays_quiet_when_no_motion_was_commanded():
    det = BlockedDetector()
    for i in range(20):
        assert not det.update(i * 0.1, 0.0, Pose(0.0, 0.0, 0.0))


