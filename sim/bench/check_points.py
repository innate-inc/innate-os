#!/usr/bin/env python3
"""Is a world point actually reachable, or is the planner too conservative?

The gate reports "no path to (x, y)" for a cluster of apartment challenges. That
is only a finding if the point really is off the navigable floor -- which is the
failure sim/challenges/30_shepherd.py warns about, since the collision plane
extends past the walls and a prop can settle upright outside the apartment. If
instead the floor is fine and obstacle inflation swallowed it, the bug is mine.

Prints, for each point: the raw grid cell, the inflated cell, and how far the
nearest usable cell is.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mars_sim_driver.core import VirtualMars  # noqa: E402
from navplan import NavMap  # noqa: E402

POINTS = [(-4.86, -3.79), (-5.30, 4.41), (-4.62, -3.98), (-4.34, -0.17)]


# In main() so that importing this module does not build a world.
def main() -> int:
    mars = VirtualMars(render_wh=(160, 120))
    grid, ox, oy = mars.occupancy_grid(0.06)
    nav = NavMap(grid, ox, oy, 0.06)
    print(f"grid {grid.shape}  origin ({ox:.2f}, {oy:.2f})  res 0.06")
    print(f"cells: free={int((grid == 0).sum())} occupied={int((grid == 100).sum())} unknown={int((grid == -1).sum())}")
    print(f"inflated blocked: {int(nav.blocked.sum())} of {nav.blocked.size}\n")

    sx, sy, _ = mars.pose()
    print(f"robot spawn ({sx:.2f}, {sy:.2f})  free={nav.free(*nav.to_cell(sx, sy))}\n")

    for x, y in POINTS:
        r, c = nav.to_cell(x, y)
        inb = nav.in_bounds(r, c)
        raw = int(grid[r, c]) if inb else None
        infl = bool(nav.blocked[r, c]) if inb else None
        nf = nav.nearest_free(x, y)
        d = None
        if nf:
            wx, wy = nav.to_world(*nf)
            d = ((wx - x) ** 2 + (wy - y) ** 2) ** 0.5
        path = nav.plan((sx, sy), (x, y))
        print(
            f"({x:6.2f},{y:6.2f})  in_bounds={inb}  raw={raw!s:>4}  inflated_blocked={infl!s:>5}  "
            f"nearest_free={f'{d:.2f}m' if d is not None else 'NONE'}  path={'yes' if path else 'no'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
