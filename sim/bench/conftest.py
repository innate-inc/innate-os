"""pytest wiring for the bench's offline tests.

One sys.path setup for every test module instead of a per-file insert: the
bench modules themselves, the sim driver (mars_sim_driver), and the brain
client package (brain_client). MUJOCO_GL defaults to the software renderer
so the one test that builds a world runs headless.

    sim/.venv/bin/python -m pytest sim/bench -q
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
REPO = BENCH.parents[1]

for path in (
    BENCH,
    REPO / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver",
    REPO / "ros2_ws" / "src" / "brain" / "brain_client",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("MUJOCO_GL", "osmesa")
