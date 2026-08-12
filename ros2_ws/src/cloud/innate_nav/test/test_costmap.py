"""Costmap-aware clearance: geometry against a synthetic grid, plus sanity
checks against a real /local_costmap/costmap window captured from the sim.

No GPU, no simulator, no ROS.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from innate_nav.costmap import INSCRIBED, LETHAL, UNKNOWN, Costmap
from innate_nav.policy_client import Pose
from innate_nav.pursuit import PursuitCfg, avoid, odom_path_from, speed_scale, to_odom, truncate_to, velocity

FIXTURE = Path(__file__).parent / "fixtures" / "local_costmap.json"


def _grid(h=40, w=40, res=0.05, ox=0.0, oy=0.0):
    return np.zeros((h, w), dtype=np.int16), res, ox, oy


def _straight(n=8, step=0.25):
    """Policy-shaped path: 8 points, 0.25 m apart, straight ahead."""
    return np.array([[step * (i + 1), 0.0, 0.0] for i in range(n)])


# --- cost lookup -------------------------------------------------------------

def test_cost_at_maps_odom_metres_to_cells():
    g, res, ox, oy = _grid()
    g[10, 20] = LETHAL
    cm = Costmap(g, res, ox, oy)
    assert cm.cost_at(20 * res + res / 2, 10 * res + res / 2) == LETHAL
    assert cm.cost_at(0.0, 0.0) == 0


def test_cost_at_honours_a_nonzero_origin():
    g, res, _, _ = _grid()
    g[0, 0] = LETHAL
    cm = Costmap(g, res, -6.2, -4.4)   # the real window's origin
    assert cm.cost_at(-6.2 + res / 2, -4.4 + res / 2) == LETHAL
    assert cm.cost_at(0.0, 0.0) == UNKNOWN      # outside a 2x2 m window


def test_outside_the_window_is_unknown_not_blocked():
    cm = Costmap(*_grid())
    assert cm.cost_at(100.0, 100.0) == UNKNOWN
    assert not cm.blocked(100.0, 100.0)


def test_blocked_covers_inscribed_as_well_as_lethal():
    g, res, ox, oy = _grid()
    g[5, 5] = INSCRIBED
    cm = Costmap(g, res, ox, oy)
    assert cm.blocked(5 * res + res / 2, 5 * res + res / 2)


# --- safe length -------------------------------------------------------------

def test_open_path_is_fully_safe():
    cm = Costmap(*_grid(h=80, w=80))
    path = odom_path_from(_straight(), Pose(0.5, 0.5, 0.0))
    assert cm.safe_length(path) == pytest.approx(2.0, abs=0.05)


def test_safe_length_stops_at_a_wall():
    g, res, ox, oy = _grid(h=80, w=80)
    g[:, 30:] = LETHAL                       # wall at x = 1.5 m
    cm = Costmap(g, res, ox, oy)
    path = odom_path_from(_straight(), Pose(0.0, 0.5, 0.0))
    assert cm.safe_length(path) == pytest.approx(1.5, abs=0.05)


def test_safe_length_catches_an_obstacle_between_waypoints():
    """The policy's points are 0.25 m apart — five cells. Testing only the
    vertices would step straight over a thin obstacle."""
    g, res, ox, oy = _grid(h=80, w=80)
    g[:, 22] = LETHAL                        # one cell thick, at x = 1.10 m
    cm = Costmap(g, res, ox, oy)
    path = odom_path_from(_straight(), Pose(0.0, 0.5, 0.0))
    assert cm.safe_length(path) < 1.2


def test_a_path_starting_inside_an_obstacle_has_no_safe_length():
    g, res, ox, oy = _grid(h=80, w=80)
    g[:, :] = LETHAL
    cm = Costmap(g, res, ox, oy)
    path = odom_path_from(_straight(), Pose(0.5, 0.5, 0.0))
    assert cm.safe_length(path) == 0.0


# --- truncation --------------------------------------------------------------

def test_truncate_keeps_the_prefix_and_interpolates_the_cut():
    wp = _straight()
    out = truncate_to(wp, 0.6)
    assert len(out) == 3                       # 0.25, 0.50, then the cut
    assert out[-1][0] == pytest.approx(0.6)


def test_truncate_to_zero_yields_an_empty_path():
    assert len(truncate_to(_straight(), 0.0)) == 0


def test_truncate_beyond_the_path_returns_it_whole():
    wp = _straight()
    assert len(truncate_to(wp, 99.0)) == len(wp)


# --- repulsion / corner widening --------------------------------------------

def test_repulsion_points_away_from_an_obstacle():
    g, res, ox, oy = _grid(h=80, w=80)
    g[:, 40:] = LETHAL                        # wall on the +x side of x = 2.0
    cm = Costmap(g, res, ox, oy)
    rep = cm.repulsion(1.9, 1.0)
    assert rep[0] < 0                          # pushed back in -x, away
    assert abs(np.hypot(*rep) - 1.0) < 1e-6


def test_repulsion_is_zero_in_open_space():
    cm = Costmap(*_grid(h=80, w=80))
    assert np.allclose(cm.repulsion(1.0, 1.0), 0.0)


def test_avoid_shifts_the_target_perpendicular_to_the_bearing():
    tgt = np.array([1.0, 0.0, 0.0])            # straight ahead
    out = avoid(tgt, np.array([0.0, 1.0]))     # obstacle pushing to +y (left)
    assert out[1] > 0.0                        # target moved left
    assert out[0] == pytest.approx(1.0)        # bearing distance unchanged


def test_avoid_ignores_repulsion_along_the_bearing():
    """Braking is speed_scale's job; avoid only steers."""
    tgt = np.array([1.0, 0.0, 0.0])
    out = avoid(tgt, np.array([-1.0, 0.0]))    # pushes straight back
    assert np.allclose(out[:2], tgt[:2])


def test_a_shaved_corner_gets_widened():
    """A convex corner with inflation around it: the raw path grazes it, and the
    repulsion at the steering target pushes the target to the open side."""
    g, res, ox, oy = _grid(h=80, w=80)
    g[:20, :20] = LETHAL                       # corner block over x,y < 1.0
    for r in range(20, 26):                    # inflation skirt above it
        g[r, :26] = 80
    cm = Costmap(g, res, ox, oy)
    # driving +x along y = 1.15, clipping the corner's inflation
    rep = cm.repulsion(0.9, 1.15)
    assert rep[1] > 0                          # pushed away from the block (+y)
    shifted = avoid(np.array([0.75, 0.0, 0.0]), rep)
    assert shifted[1] > 0.02                   # target moved to the open side


# --- speed scaling -----------------------------------------------------------

def test_speed_is_unscaled_in_the_open():
    assert speed_scale(0.0) == 1.0


def test_speed_tapers_inside_the_inflation_band():
    cfg = PursuitCfg()
    mid = speed_scale(cfg.slow_cost / 2, cfg)
    assert 0.0 < mid < 1.0


def test_speed_never_reaches_zero_however_close_the_path_runs():
    """The regression that froze the robot solid. truncate_to puts the cut point
    ON the first blocked cell, so the kept path's worst cost is ~INSCRIBED
    whenever anything was in the way — indoors, essentially always. A speed term
    that bottoms out at zero then vetoes all motion forever, even though
    safe_length said there was over a metre of clear path ahead."""
    cfg = PursuitCfg()
    for cost in (cfg.slow_cost, INSCRIBED, LETHAL, 1e6):
        assert speed_scale(cost, cfg) >= cfg.min_speed_scale > 0.0


def test_speed_bottoms_out_at_the_floor_not_below():
    cfg = PursuitCfg()
    assert speed_scale(cfg.slow_cost, cfg) == pytest.approx(cfg.min_speed_scale)


def test_a_safe_prefix_always_yields_forward_motion():
    """The invariant tying the hard and soft terms together: if safe_length
    leaves anything to drive on, the robot must actually drive."""
    g, res, ox, oy = _grid(h=80, w=80)
    g[:, 30:] = LETHAL                          # wall 1.5 m ahead
    for c in range(24, 30):                     # its inflation skirt
        g[:, c] = 95
    cm = Costmap(g, res, ox, oy)
    pose = Pose(0.0, 0.5, 0.0)
    wp = _straight()
    safe = cm.safe_length(odom_path_from(wp, pose))
    assert safe > 0.5, "sanity: there is real room ahead"
    kept = truncate_to(wp, safe)
    scale = speed_scale(cm.worst_cost(odom_path_from(kept, pose)))
    assert scale > 0.0
    v, _w = velocity(kept)
    assert v * scale > 0.0


# --- against the real captured window ---------------------------------------

@pytest.mark.skipif(not FIXTURE.exists(), reason="no captured costmap fixture")
def test_real_window_parses_and_matches_its_header():
    cm = Costmap.from_msg(json.loads(FIXTURE.read_text()))
    assert cm.grid.shape == (120, 120)
    assert cm.resolution == pytest.approx(0.05, abs=1e-6)
    assert cm.origin_x == pytest.approx(-6.2)
    assert cm.origin_y == pytest.approx(-4.4)


@pytest.mark.skipif(not FIXTURE.exists(), reason="no captured costmap fixture")
def test_real_window_has_free_space_lethal_cells_and_an_inflation_gradient():
    cm = Costmap.from_msg(json.loads(FIXTURE.read_text()))
    vals = set(np.unique(cm.grid).tolist())
    assert 0 in vals and LETHAL in vals and INSCRIBED in vals
    assert any(0 < v < INSCRIBED for v in vals), "no inflation band to descend"


@pytest.mark.skipif(not FIXTURE.exists(), reason="no captured costmap fixture")
def test_real_window_blocks_a_path_driven_into_a_lethal_cell():
    cm = Costmap.from_msg(json.loads(FIXTURE.read_text()))
    rows, cols = np.nonzero(cm.grid >= INSCRIBED)
    # aim from 1 m away straight at a real occupied cell
    tx = cm.origin_x + (cols[0] + 0.5) * cm.resolution
    ty = cm.origin_y + (rows[0] + 0.5) * cm.resolution
    start = Pose(tx - 1.0, ty, 0.0)
    path = to_odom(_straight(), start)
    assert cm.safe_length(path) < 2.0, "a path into an obstacle must be cut short"


@pytest.mark.skipif(not FIXTURE.exists(), reason="no captured costmap fixture")
def test_real_window_repulsion_pushes_off_occupied_cells():
    cm = Costmap.from_msg(json.loads(FIXTURE.read_text()))
    rows, cols = np.nonzero(cm.grid >= INSCRIBED)
    pushed = 0
    for r, c in list(zip(rows, cols, strict=True))[:200]:
        ox = cm.origin_x + (c + 0.5) * cm.resolution
        oy = cm.origin_y + (r + 0.5) * cm.resolution
        # stand 0.2 m away and check the push has a component away from the cell
        for dx, dy in ((0.2, 0.0), (-0.2, 0.0), (0.0, 0.2), (0.0, -0.2)):
            rep = cm.repulsion(ox + dx, oy + dy)
            if np.hypot(*rep) > 0 and np.dot(rep, (dx, dy)) > 0:
                pushed += 1
    assert pushed > 100, "repulsion should point away from real obstacles"


@pytest.mark.skipif(not FIXTURE.exists(), reason="no captured costmap fixture")
def test_velocity_still_drives_when_the_truncated_path_is_open():
    cm = Costmap.from_msg(json.loads(FIXTURE.read_text()))
    free = np.argwhere(cm.grid == 0)
    r, c = free[len(free) // 2]
    pose = Pose(cm.origin_x + (c + 0.5) * cm.resolution,
                cm.origin_y + (r + 0.5) * cm.resolution, 0.0)
    wp = _straight()
    safe = cm.safe_length(odom_path_from(wp, pose))
    kept = truncate_to(wp, safe)
    if len(kept) >= 2:
        v, _w = velocity(kept)
        assert v >= 0.0
