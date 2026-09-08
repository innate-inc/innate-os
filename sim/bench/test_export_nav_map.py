"""The nav map keeps authored floor outside the walls, and the shipped blaze map has its porch.

Every blaze challenge ends on the porch, outside the south wall. The exporter
clips the scan to the wall envelope so the exterior ground plane never becomes
navigable, and that clip took the porch with it: all 627 of its cells were
unknown in the shipped map, which `allow_unknown: false` refuses to plan
into. A floor slab -- a collidable, level box whose top is the floor -- is as
deliberate as a wall, so it joins the envelope. The ground plane under it does
not.
"""

from __future__ import annotations

if __name__ == "__main__":  # run directly: let pytest collect this file (conftest.py sets sys.path)
    import sys

    import pytest

    raise SystemExit(pytest.main([__file__] + sys.argv[1:]))

import re
from pathlib import Path

import numpy as np
import pytest
from export_nav_map import RESOLUTION, floor_slabs, slab_cells
from mars_sim_driver.statics import Geom, Room, RoomRegistry

REPO = Path(__file__).resolve().parents[2]
PORCH = (-3.25, -3.3, -1.65, -2.4)  # sim/bundles/blaze/challenges/_zones.py


def test_only_level_collidable_slabs_at_floor_height_are_floor() -> None:
    room = Room(
        "r",
        geoms=[
            Geom("box", (1.0, 0.55, 0.05), (-2.45, -2.85, -0.05), name="porch"),
            Geom("box", (3.3, 0.05, 1.2), (0.0, -2.35, 1.2), name="south wall"),
            Geom("box", (1.0, 1.0, 0.05), (0.0, 0.0, -0.05), name="seam", collide=False),
            Geom("box", (1.0, 1.0, 0.05), (0.0, 0.0, -0.05), quat=(0.7071, 0.7071, 0.0, 0.0), name="tilted"),
            Geom("box", (0.4, 0.4, 0.4), (1.0, 1.0, 0.4), name="table"),
            Geom("cylinder", (0.5, 0.02), (2.0, 2.0, -0.02), name="round rug"),
        ],
    )
    slabs = floor_slabs(RoomRegistry({"r": room}))
    assert slabs == [pytest.approx((-3.45, -3.4, -1.45, -2.3))]


def test_slabs_become_inclusive_cell_boxes_clipped_to_the_grid() -> None:
    # Edges off the cell boundaries, so the expectation does not hinge on
    # floating-point floor(31.0000001) versus floor(30.9999999).
    cells = list(slab_cells([(-3.42, -3.38, -1.48, -2.33), (50.0, 50.0, 51.0, 51.0)], -5.0, -5.0, (100, 100)))
    assert cells == [(32, 53, 31, 70)]
    assert RESOLUTION == 0.05


def _shipped_blaze_map() -> tuple[np.ndarray, float, float, float]:
    folder = REPO / "sim" / "environments" / "blaze" / "map"
    text = (folder / "blaze.yaml").read_text()
    res = float(re.search(r"resolution:\s*([\d.]+)", text).group(1))
    ox, oy = (float(v) for v in re.search(r"origin:\s*\[([^\]]+)\]", text).group(1).split(",")[:2])
    data = (folder / "blaze.pgm").read_bytes()
    header = re.match(rb"P5\s+(\d+)\s+(\d+)\s+(\d+)\s", data)
    w, h, _ = (int(v) for v in header.groups())
    return np.frombuffer(data[header.end() :], dtype=np.uint8).reshape(h, w), res, ox, oy


def test_the_shipped_blaze_map_has_its_porch() -> None:
    """Regenerated with `export_nav_map.py --environment blaze --out
    environments/blaze/map`; an older exporter ships it unknown again."""
    img, res, ox, oy = _shipped_blaze_map()
    h = img.shape[0]
    x0, y0, x1, y1 = PORCH
    rows = slice(h - 1 - int((y1 - oy) / res), h - int((y0 - oy) / res))
    cols = slice(int((x0 - ox) / res), int((x1 - ox) / res) + 1)
    porch = img[rows, cols]
    free = (porch == 254).mean()
    assert free > 0.8, f"only {free:.0%} of the porch is free in the shipped blaze map"
    # And the ground plane beyond the porch's edge is still unknown, not floor.
    beyond = img[rows, cols.stop + 8 : cols.stop + 20]
    assert (beyond == 205).all(), "the exterior past the porch has become navigable"
