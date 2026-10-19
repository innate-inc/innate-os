"""The path simplifier must reject exactly the shortcuts that hit something.

Cells are inflated by the robot radius, so a cell the check misses is one the
robot's body would clip -- and a cell it invents rejects a shortcut that was
fine, which changes waypoint counts and path lengths on runs used as a
reference. Both directions matter, so this compares `_clear_line` against the
geometry itself over every endpoint pair in a grid, rather than against a
handful of cases I happened to think of.

Ground truth: cell (r, c) is touched when the segment between the two cell
CENTRES intersects the closed unit square around (r, c).
"""

import sys
from itertools import product
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from navplan import NavMap, _clear_line


def nav(grid):
    """A NavMap over a literal grid, inflation off: this is a test of the line
    check, not of the radius that usually masks its mistakes."""
    m = NavMap.__new__(NavMap)
    m.blocked = np.array(grid, dtype=bool)
    m.h, m.w = m.blocked.shape
    m.resolution = 0.05
    m.origin = (0.0, 0.0)
    return m


def touches(a, b, cell):
    """Does the segment a->b (cell centres) meet the unit square at `cell`?"""
    (r0, c0), (r1, c1), (r, c) = a, b, cell
    lo = (r - 0.5, c - 0.5)
    hi = (r + 0.5, c + 0.5)
    t0, t1 = 0.0, 1.0
    for p, q, lo_i, hi_i in ((r0, r1 - r0, lo[0], hi[0]), (c0, c1 - c0, lo[1], hi[1])):
        if q == 0:
            if p < lo_i or p > hi_i:
                return False
            continue
        ta, tb = (lo_i - p) / q, (hi_i - p) / q
        if ta > tb:
            ta, tb = tb, ta
        t0, t1 = max(t0, ta), min(t1, tb)
        if t0 > t1 + 1e-12:
            return False
    return True


def test_matches_the_geometry_on_every_pair_in_a_5x5():
    """One blocked cell at a time, every endpoint pair: the check must refuse
    exactly when the segment really touches that cell."""
    n = 5
    cells = list(product(range(n), repeat=2))
    wrong = []
    for blocked in cells:
        grid = np.zeros((n, n), dtype=bool)
        grid[blocked] = True
        m = nav(grid)
        for a, b in product(cells, repeat=2):
            if a == blocked or b == blocked:
                continue  # an endpoint on an obstacle is a different question
            got = _clear_line(a, b, m)
            want = not touches(a, b, blocked)
            if got != want:
                wrong.append((a, b, blocked, got, want))
    assert not wrong, f"{len(wrong)} disagreements, e.g. {wrong[:4]}"


def test_matches_the_geometry_on_non_square_grids():
    """The nav grid is not square, and a traversal that hardcodes the driving
    axis one way is easy to get right on a square and wrong on a strip."""
    for shape in ((4, 7), (7, 3)):
        h, w = shape
        cells = list(product(range(h), range(w)))
        for blocked in cells:
            grid = np.zeros(shape, dtype=bool)
            grid[blocked] = True
            m = nav(grid)
            for a, b in product(cells, repeat=2):
                if a == blocked or b == blocked:
                    continue
                assert _clear_line(a, b, m) == (not touches(a, b, blocked)), (shape, a, b, blocked)


def test_is_symmetric():
    """A path is clear or it is not; which end you start from cannot decide it.
    Bresenham-derived walks disagree here easily, and the simplifier calls this
    with whichever pair of waypoints it happens to hold."""
    n = 5
    cells = list(product(range(n), repeat=2))
    for blocked in cells:
        grid = np.zeros((n, n), dtype=bool)
        grid[blocked] = True
        m = nav(grid)
        for a, b in product(cells, repeat=2):
            assert _clear_line(a, b, m) == _clear_line(b, a, m), (a, b, blocked)


def test_the_case_that_caught_the_over_strict_version():
    #  (0,0)->(1,2) crosses into column 2 at row 1.25, so it never enters (0,2)
    m = nav([[0, 0, 1], [0, 0, 0]])
    assert touches((0, 0), (1, 2), (1, 1)) and not touches((0, 0), (1, 2), (0, 2))
    assert _clear_line((0, 0), (1, 2), m), "rejected a shortcut that is clear"


def test_diagonal_pinch_is_still_refused():
    #   . X
    #   X .     the segment passes exactly through the shared corner
    assert not _clear_line((0, 0), (1, 1), nav([[0, 1], [1, 0]]))
    assert not _clear_line((0, 1), (1, 0), nav([[0, 1], [1, 0]]))


def test_endpoints_and_degenerate():
    m = nav([[0, 0, 0], [0, 1, 0], [0, 0, 0]])
    assert not _clear_line((0, 1), (2, 1), m), "walked through a blocked cell"
    assert not _clear_line((1, 1), (1, 1), m), "a blocked cell is not clear of itself"
    assert _clear_line((0, 0), (0, 0), m)
    assert _clear_line((0, 0), (2, 0), m)
