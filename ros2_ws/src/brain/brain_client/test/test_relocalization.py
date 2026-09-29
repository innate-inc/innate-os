# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Lidar relocalization must refuse a scan that fits more than one pose, and commit to the one that fits alone.

Synthetic rooms and scans raycast from a known pose. A closed rectangle is
symmetric under a half turn about its centre, so a scan taken there fits the
reverse heading at the same spot equally well (a corridor does the same);
walling off one corner into an L breaks the tie.
"""

import math
from types import SimpleNamespace

import numpy as np

from brain_client.relocalization.hypotheses import decide
from brain_client.relocalization.scan_match import LASER_X_M, Grid, Scan
from brain_client.state.lidar import Lidar
from brain_client.state.map import Map

RES = 0.05
BEAMS = 720


def _room(*, l_shaped: bool) -> np.ndarray:
    cells = np.full((80, 140), -1, np.int8)  # 4 m x 7 m, row index grows with +y
    cells[10:70, 10:130] = 100
    cells[11:69, 11:129] = 0
    if l_shaped:
        cells[11:40, 90:129] = 100
    return cells


def _grid(cells: np.ndarray) -> Grid:
    h, w = cells.shape
    grid = Grid.from_map(Map(RES, w, h, 0.0, 0.0, raw_source=SimpleNamespace(data=cells.ravel().tolist())))
    assert grid is not None
    return grid


def _scan(cells: np.ndarray, x: float, y: float, theta: float) -> Scan:
    """What the lidar sees from base pose (x, y, theta): the first wall along each beam."""
    lx, ly = x + LASER_X_M * math.cos(theta), y + LASER_X_M * math.sin(theta)
    increment = 2 * math.pi / BEAMS
    beams = theta - math.pi + np.arange(BEAMS) * increment
    r = np.arange(1, 800)[:, None] * (RES / 4)
    rows = np.clip(((ly + r * np.sin(beams)) / RES).astype(int), 0, cells.shape[0] - 1)
    cols = np.clip(((lx + r * np.cos(beams)) / RES).astype(int), 0, cells.shape[1] - 1)
    ranges = r[(cells[rows, cols] >= 65).argmax(axis=0), 0]
    return Scan.from_lidar(Lidar(tuple(ranges.tolist()), -math.pi, increment, 0.05, 12.0))


def test_the_reverse_heading_at_the_same_spot_is_refused():
    cells = _room(l_shaped=False)
    centre_x, centre_y = 3.5, 2.0
    decision = decide(_grid(cells), _scan(cells, centre_x - LASER_X_M, centre_y, 0.0))
    assert decision.pose is None, f"committed to one of two equally good headings: {decision.reason}"


def test_a_scan_that_fits_one_pose_localizes_there():
    cells = _room(l_shaped=True)
    x, y, theta = 2.0, 2.6, 0.6
    decision = decide(_grid(cells), _scan(cells, x, y, theta))
    assert decision.pose is not None, decision.reason
    assert math.hypot(decision.pose.x - x, decision.pose.y - y) < 0.1
    assert abs(math.remainder(decision.pose.theta - theta, math.tau)) < math.radians(3)
