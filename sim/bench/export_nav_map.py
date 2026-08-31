"""Export the loaded world's occupancy map as a nav2 map_server map
(sim_apartment.yaml + .pgm) into sim/assets/map/. The tmux launch script
seeds it into $INNATE_OS_ROOT/data/maps so the mode manager boots straight
into navigation mode with it.

Usage: cd sim && uv run bench/export_nav_map.py
       (honours VIRTUAL_MARS_ASSETS, so it exports whichever bundle is loaded)

TWO FAULTS THIS FILE USED TO SHIP, both of which made the planner believe it
could drive where the robot cannot go. See sim/bench/lint_navmap.py, which
fails the build on the first and is the regression test for the second.

1. UNKNOWN READ AS FREE. nav2 does not read greys, it reads a probability:
   occ = (255 - grey)/255, occupied above occupied_thresh, free below
   free_thresh, unknown in between. Unknown is grey 205, so occ = 0.19608, and
   the ROS convention pairs it with free_thresh 0.196 precisely so that value
   lands just ABOVE the line and stays unknown. This wrote 0.25, so every
   unknown cell came back as free floor -- and planner.yaml's `allow_unknown:
   false` had nothing left to refuse.

2. THE WORLD OUTSIDE THE BUILDING, SCANNED. Scan origins were every free cell
   of the collision grid, whose bounds are the whole model -- including the
   ground plane stretching past the walls. Casting from out there paints the
   exterior as genuine free floor: on the cafe bundle, 78% of the map was
   open apron the robot can never reach, most of it with better clearance than
   the furnished interior. A goal near the doorway then has two candidate
   routes, one through the room and one out and around, and the planner can
   prefer either as live scans re-price the obstacle layer. Origins are now
   restricted to what the robot can actually reach from where it spawns, and
   anything left unscanned stays unknown.

The bug is not visible on a map whose bounds hug its walls, which is why it
survived: it needs a world with space outside the building to show up.
"""

import math
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sandbox"))
import _driver_pkg  # noqa: F401
from mars_sim_driver.core import VirtualMars

RESOLUTION = 0.05
# Strictly below (255-205)/255 = 0.19608, so grey 205 stays unknown.
FREE_THRESH = 0.196


def reachable_from(grid: np.ndarray, seed: tuple[int, int]) -> np.ndarray:
    """Mask of free cells 4-connected to `seed`.

    4-connected on purpose: an 8-connected fill leaks diagonally between two
    obstacle cells that touch only at a corner, which is exactly the gap a
    footprint cannot pass through."""
    reach = np.zeros(grid.shape, dtype=bool)
    if not (0 <= seed[0] < grid.shape[0] and 0 <= seed[1] < grid.shape[1]) or grid[seed] != 0:
        return reach
    reach[seed] = True
    queue = deque([seed])
    while queue:
        r, c = queue.popleft()
        for nr, nc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
            if 0 <= nr < grid.shape[0] and 0 <= nc < grid.shape[1] and grid[nr, nc] == 0 and not reach[nr, nc]:
                reach[nr, nc] = True
                queue.append((nr, nc))
    return reach


def outer_wall_bbox(grid: np.ndarray) -> tuple[int, int, int, int] | None:
    """Bounding box of the largest occupied component -- the building's walls.

    8-connected, because a wall drawn by a raycast is a one-cell line that
    turns corners diagonally; a 4-connected pass splits it into segments and
    picks one side of the room."""
    occupied = grid == 100
    if not occupied.any():
        return None
    seen = np.zeros(grid.shape, dtype=bool)
    best: list[tuple[int, int]] = []
    for r, c in zip(*np.nonzero(occupied), strict=True):
        if seen[r, c]:
            continue
        queue, cells = deque([(int(r), int(c))]), []
        seen[r, c] = True
        while queue:
            y, x = queue.popleft()
            cells.append((y, x))
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < grid.shape[0] and 0 <= nx < grid.shape[1] and occupied[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        queue.append((ny, nx))
        if len(cells) > len(best):
            best = cells
    rows = [r for r, _ in best]
    cols = [c for _, c in best]
    return min(rows), max(rows), min(cols), max(cols)


def main() -> int:
    sim = VirtualMars()
    # Lidar-consistent map (virtual SLAM at the laser's true height): AMCL
    # localizes against what the lidar actually returns, exactly like a real
    # robot localizing against its own SLAM map. occupancy_grid() (collision
    # slab) systematically disagrees with the lidar around furniture and
    # walks AMCL off the map.
    grid, ox, oy = sim.lidar_occupancy_grid(RESOLUTION)

    # Keep only what the robot could reach from where it stands, INSIDE the
    # building. Reachability alone is not enough: a 4-connected fill has no
    # notion of an envelope, so on any world with a doorway it walks out
    # through the 0.6m gap and keeps the whole exterior ground plane. Measured
    # before this clip: counter 0% apron (its room is sealed, which is why the
    # fix looked general), but pantry 79%, blaze 81%, the shipped apartment
    # 74%. The exterior IS traversable in the physics world, so this is not a
    # correctness fix to the scan -- it is a decision that the nav map covers
    # the building, so the planner cannot answer "go to the far corner" with a
    # stroll around the outside.
    rx, ry, _yaw = sim.pose()
    seed = (int(math.floor((ry - oy) / RESOLUTION)), int(math.floor((rx - ox) / RESOLUTION)))
    envelope = outer_wall_bbox(grid)
    if envelope is None:
        print("ERROR: no occupied cells at all; refusing to export a map with no walls")
        return 1
    r0, r1, c0, c1 = envelope
    if not (r0 <= seed[0] <= r1 and c0 <= seed[1] <= c1):
        print(
            f"ERROR: robot cell {seed} is outside the wall envelope rows {r0}-{r1} cols {c0}-{c1}; refusing to export"
        )
        return 1
    bounded = grid.copy()
    outside = np.ones(grid.shape, dtype=bool)
    outside[r0 : r1 + 1, c0 : c1 + 1] = False
    bounded[outside] = -1  # the apron is not part of the building
    reach = reachable_from(bounded, seed)
    if not reach.any():
        # The robot is not standing on a scanned free cell (spawned on a pad,
        # or the scan missed its footprint). Exporting the raw grid would
        # quietly ship the apron again, so FAIL rather than paper over it:
        # returning 0 here let a broken map through to the launcher, which
        # copies it without looking.
        print(f"ERROR: robot cell {seed} is not free in the scan; refusing to export")
        return 1
    dropped = int((grid == 0).sum() - reach.sum())
    grid = np.where(reach, 0, np.where(grid == 100, 100, -1)).astype(np.int8)
    # An obstacle only matters if it bounds somewhere reachable; the far side
    # of an exterior wall is scenery, and leaving it in invites the planner to
    # hug it. Keep occupied cells adjacent to reachable free space.
    # Padded shifts, not np.roll: roll WRAPS, so a free cell on the top row
    # would keep an obstacle on the bottom row of the same column -- a
    # false-keep at the opposite edge. Latent on today's worlds (measured: zero
    # cells kept only by wrapping, on all nine) but wrong, and invisible.
    padded = np.pad(reach, 1)
    keep = np.zeros(grid.shape, dtype=bool)
    for dr in (0, 1, 2):
        for dc in (0, 1, 2):
            keep |= padded[dr : dr + grid.shape[0], dc : dc + grid.shape[1]]
    grid[(grid == 100) & ~keep] = -1

    # map_server PGM: 254 free, 0 occupied, 205 unknown; row 0 at the TOP.
    img = np.where(grid == 100, 0, np.where(grid == 0, 254, 205)).astype(np.uint8)[::-1]
    out = Path(__file__).resolve().parents[1] / "assets" / "map"
    out.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(out / "sim_apartment.pgm")
    (out / "sim_apartment.yaml").write_text(
        f"image: sim_apartment.pgm\nmode: trinary\nresolution: {RESOLUTION}\n"
        f"origin: [{ox:.4f}, {oy:.4f}, 0.0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: {FREE_THRESH}\n"
    )
    free = int((grid == 0).sum())
    print(
        f"wrote {out}/sim_apartment.yaml ({grid.shape[1]}x{grid.shape[0]} @ {RESOLUTION}m): "
        f"{free} free, {int((grid == 100).sum())} occupied, {int((grid == -1).sum())} unknown "
        f"({dropped} unreachable cells returned to unknown)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
