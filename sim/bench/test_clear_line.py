"""The path simplifier must not shortcut through a diagonal gap.

The planner's cells are inflated by the robot radius, so a corner the check
misses is a corner the robot's body would hit. These pin the supercover
property directly, without a world: a diagonal pinch is blocked, and the
straight/clear cases still simplify.
"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from navplan import NavMap, _clear_line


def nav(grid):
    """NavMap over a literal grid, with inflation off so the test is about the
    line check and not about the radius that usually masks it."""
    blocked = np.array(grid, dtype=bool)
    m = NavMap.__new__(NavMap)
    m.blocked = blocked
    m.h, m.w = blocked.shape
    m.resolution = 0.05
    m.origin = (0.0, 0.0)
    return m


def test_diagonal_pinch_is_not_clear():
    #   . X
    #   X .      the segment (0,0)->(1,1) clips both blocked corners
    m = nav([[0, 1], [1, 0]])
    assert not _clear_line((0, 0), (1, 1), m), "cut the corner between two obstacles"


def test_open_diagonal_is_clear():
    m = nav([[0, 0], [0, 0]])
    assert _clear_line((0, 0), (1, 1), m)


def test_blocked_endpoint_and_midpoint():
    m = nav([[0, 0, 0], [0, 1, 0], [0, 0, 0]])
    assert not _clear_line((0, 1), (2, 1), m), "walked through a blocked cell"
    assert not _clear_line((1, 1), (1, 1), m), "a blocked cell is not clear of itself"
    assert _clear_line((0, 0), (2, 0), m)


def test_single_cell_is_itself():
    assert _clear_line((0, 0), (0, 0), nav([[0]]))


def test_shallow_diagonal_still_walks_every_row():
    #  a nearly-horizontal line must still notice a cell it only clips
    m = nav([[0, 0, 0, 0], [0, 0, 1, 0]])
    assert not _clear_line((0, 0), (1, 3), m)
