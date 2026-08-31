#!/usr/bin/env python3
"""Check an exported nav2 map for the unknown-read-as-free fault.

THE FAULT. `nav_grid` builds a lidar-consistent map correctly: cells start at
-1 (unknown), beams clear to 0 (free), returns mark 100 (occupied). The
exporter writes those as the conventional 254 / 0 / 205 greys. But nav2 does
not read greys, it reads a probability:

    occ = (255 - grey) / 255          # negate: 0
    occ > occupied_thresh -> occupied
    occ < free_thresh     -> free
    otherwise             -> unknown  # trinary

Unknown is grey 205, so occ = 0.19608. The ROS convention pairs it with
free_thresh 0.196 -- chosen so that 0.19608 lands just ABOVE the line and stays
unknown. Ship any larger threshold and every unknown cell silently becomes free
floor.

WHY IT MATTERS. Unknown is everything the virtual lidar never saw: the whole
world outside the room walls. Read as free, the planner believes it can drive
out through the doorway and around the OUTSIDE of the building, so a goal
across the room has two candidate routes with different lengths. Each replan
can pick the other one, `distance_remaining` steps between two values several
metres apart, the controller drives at a wall it thinks is open, recovery
behaviours fire, and progress sits at 0%.

The map that provokes it is not the one that reveals it: a map whose bounds hug
its walls has almost no unknown region, so the same bug is invisible. Bounds
that extend past the walls make it fatal.

  usage: lint_navmap.py <map.yaml>
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import numpy as np

UNKNOWN_GREY = 205
ROS_FREE_THRESH = 0.196
# A few percent of slack: the envelope is the bounding box of the outer-wall
# component, so a wall that is not perfectly rectangular leaves legitimate
# interior floor just outside the box.
APRON_LIMIT_PCT = 5.0


def apron_area(free: np.ndarray, res: float) -> float:
    """Square metres of drivable floor OUTSIDE the building.

    Exterior is defined as drivable floor connected to the edge of the map. A
    building is enclosed by its own walls, so its interior cannot reach the
    border without passing through one; the open ground plane around it always
    can. That holds however the walls are arranged.

    The first version of this used the bounding box of the largest occupied
    component as the envelope, and it was wrong on exactly the case it needed
    to get right: on `blaze`, a multi-room house, the largest wall component is
    an INTERIOR run whose box does not cover the building, so 3.7 m2 of
    perfectly ordinary bedroom floor was reported as outdoors and the map
    failed a lint it should have passed. The rendered map showed a clean floor
    plan with no apron at all.

    Assumes the map extends past the building. If bounds ever hug the walls,
    interior floor would touch the border and this would over-report -- the
    opposite of the earlier failure, and visible immediately rather than
    silently.
    """
    if not free.any():
        return 0.0
    height, width = free.shape
    border = [(0, c) for c in range(width)] + [(height - 1, c) for c in range(width)]
    border += [(r, 0) for r in range(height)] + [(r, width - 1) for r in range(height)]

    outside = np.zeros(free.shape, dtype=bool)
    queue: deque = deque()
    for r, c in border:
        if free[r, c] and not outside[r, c]:
            outside[r, c] = True
            queue.append((r, c))
    while queue:
        r, c = queue.popleft()
        for nr, nc in ((r - 1, c), (r + 1, c), (r, c - 1), (r, c + 1)):
            if 0 <= nr < height and 0 <= nc < width and free[nr, nc] and not outside[nr, nc]:
                outside[nr, nc] = True
                queue.append((nr, nc))
    return float(outside.sum()) * res * res


def read_yaml(path: Path) -> dict:
    """Flat scalars and one inline list -- enough for a nav2 map yaml, and it
    keeps this lint runnable in the bench venv, which has no PyYAML."""
    meta: dict = {}
    for line in path.read_text().splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        if val.startswith("["):
            meta[key.strip()] = [float(v) for v in val.strip("[]").split(",")]
        else:
            try:
                meta[key.strip()] = float(val)
            except ValueError:
                meta[key.strip()] = val
    return meta


def read_pgm(path: Path) -> np.ndarray:
    raw = path.read_bytes()
    fields: list[bytes] = []
    i = 0
    while len(fields) < 4:
        while raw[i : i + 1].isspace():
            i += 1
        if raw[i : i + 1] == b"#":
            while raw[i : i + 1] not in (b"\n", b""):
                i += 1
            continue
        j = i
        while not raw[j : j + 1].isspace():
            j += 1
        fields.append(raw[i:j])
        i = j
    w, h = int(fields[1]), int(fields[2])
    return np.frombuffer(raw[i + 1 : i + 1 + w * h], dtype=np.uint8).reshape(h, w)[::-1]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    yaml_path = Path(sys.argv[1])
    meta = read_yaml(yaml_path)
    grey = read_pgm(yaml_path.parent / meta["image"])
    res = float(meta["resolution"])
    free_thresh = float(meta["free_thresh"])

    occ = (255.0 - grey.astype(np.float32)) / 255.0
    unknown_occ = (255.0 - UNKNOWN_GREY) / 255.0

    n_unknown_grey = int((grey == UNKNOWN_GREY).sum())
    as_free = unknown_occ < free_thresh
    print(f"{yaml_path.name}: {grey.shape[1]}x{grey.shape[0]} @ {res}m, free_thresh {free_thresh}")
    print(
        f"  grey {UNKNOWN_GREY} (unknown) -> occ {unknown_occ:.5f} -> "
        f"{'FREE  <-- FAULT' if as_free else 'unknown (correct)'}"
    )
    print(f"  {n_unknown_grey:,} unknown cells ({100.0 * n_unknown_grey / grey.size:.1f}% of the map)")

    read_free = occ < free_thresh

    # THE SECOND CHECK USED TO BE VACUOUS, and it took an adversarial review to
    # notice. It compared routes over `true_free = (occ < 0.196) & (grey !=
    # 205)` against `read_free = occ < free_thresh`. But occ(205) = 0.19608 is
    # NOT below 0.196, so the `grey != 205` term excludes nothing the first
    # term kept: once free_thresh is CORRECT the two masks are bit-identical,
    # the gap is identically zero, and it reported "0 cheated cells" by
    # construction rather than by evidence. It could only ever fire in the case
    # the check above already catches.
    #
    # What actually needed testing is whether the drivable map is the BUILDING.
    # A 4-connected reachability fill walks out through a 0.6m doorway and
    # keeps the whole exterior ground plane -- measured at 79% of the pantry
    # map and 81% of blaze, while the sealed cafe read 0% and made the export
    # look fixed everywhere.
    free_m2 = float(read_free.sum()) * res * res
    apron_m2 = apron_area(read_free, res)
    apron_share = 100.0 * apron_m2 / free_m2 if free_m2 else 0.0
    print(
        f"  drivable floor {free_m2:7.1f} m2, of which {apron_m2:.1f} m2 "
        f"({apron_share:.0f}%) lies outside the outer walls"
    )

    if as_free:
        print(
            f"\nFAIL: unknown space is drivable. Set free_thresh to {ROS_FREE_THRESH} "
            f"(strictly below {unknown_occ:.5f})."
        )
        return 1
    if apron_share > APRON_LIMIT_PCT:
        print(
            f"\nFAIL: {apron_share:.0f}% of the drivable map is outside the building. The "
            f"planner can\nanswer a goal across the room by going out of the door and "
            f"around, which is the\nroute oscillation this map was supposed to remove."
        )
        return 1
    print("\nOK: unknown space is not drivable, and the drivable map is the building.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
