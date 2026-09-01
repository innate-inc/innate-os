#!/usr/bin/env python3
"""Compile the MuJoCo world once at image build time so the .mjb cache ships
baked in. Compiling 1300+ hulls costs minutes; loading the binary costs ~50ms
(mars_sim_driver.core._model_cache_path) -- the difference between a demo
session starting in seconds and starting in minutes.

Three ways to bake a cache the runtime then misses, all silent:

- Importing mars_sim_driver from ros2_ws/src instead of the sourced install
  overlay. The cache key hashes each asset's PATH as well as its mtime+size,
  and world.py/core.py are themselves assets, so the two trees key differently.
  Hence no sys.path games here: source install/setup.bash and import.
- Building the world at a different render scale than the world server will.
  The scale sets the visual-room texture cap (core._texture_cap), which is
  baked into the compiled model -- so scale 1 and scale 2 are different worlds.
- COPYing anything into the image after this runs, which moves an mtime.
"""

import os
import sys
from pathlib import Path

from mars_sim_driver import core, world
from mars_sim_driver.constants import CAMERA_HEIGHT, CAMERA_WIDTH
from mars_sim_driver.core import VirtualMars
from mars_sim_driver.world_server import DEPTH_WH

INSTALL_ROOT = Path("/root/innate-os/ros2_ws/install")
SCALE_MARKER = "render_scale"


def main() -> int:
    for module in (core, world):
        path = Path(module.__file__ or "")
        if not path.is_relative_to(INSTALL_ROOT):
            print(
                f"ERROR: {module.__name__} imported from {path}, not the install tree.\n"
                "The baked cache would key differently from the running world server.",
                file=sys.stderr,
            )
            return 1

    scale = int(os.environ.get("INNATE_SIM_RENDER_SCALE", "1"))
    assets = Path(os.environ["VIRTUAL_MARS_ASSETS"])

    # Exactly what world_server.main() builds, so the cache key matches.
    sim = VirtualMars(
        render_wh=(CAMERA_WIDTH // scale, CAMERA_HEIGHT // scale),
        depth_render_wh=DEPTH_WH,
    )
    print(f"compiled world at render scale {scale}: {sim.model.nbody} bodies, {sim.model.ngeom} geoms")

    cache_dir = assets / ".model_cache"
    cached = sorted(cache_dir.glob("world-*.mjb"))
    if not cached:
        print("ERROR: no .mjb written -- every session would compile on boot.", file=sys.stderr)
        return 1
    for path in cached:
        print(f"model cache: {path} ({path.stat().st_size / 1e6:.0f} MB)")

    # entrypoint.sh reads this to refuse a render scale the cache was not baked for.
    (cache_dir / SCALE_MARKER).write_text(f"{scale}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
