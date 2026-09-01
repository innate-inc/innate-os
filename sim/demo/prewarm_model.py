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
import time
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

    def build() -> float:
        # Exactly what world_server.main() builds, so the cache key matches.
        start = time.monotonic()
        VirtualMars(
            render_wh=(CAMERA_WIDTH // scale, CAMERA_HEIGHT // scale),
            depth_render_wh=DEPTH_WH,
        )
        return time.monotonic() - start

    compiled = build()
    print(f"world at render scale {scale}: first build {compiled:.1f}s")

    cache_dir = assets / ".model_cache"
    cached = sorted(cache_dir.glob("world-*.mjb"))
    if len(cached) != 1:
        names = [p.name for p in cached] or ["<none>"]
        print(
            f"ERROR: expected exactly one .mjb, found {len(cached)}: {names}.\n"
            "Cannot tell which one the runtime would resolve to.",
            file=sys.stderr,
        )
        return 1
    print(f"model cache: {cached[0]} ({cached[0].stat().st_size / 1e6:.0f} MB)")

    # Building a second time is the only honest proof: a .mjb sitting at some
    # other key would satisfy the glob above while the runtime still compiles
    # from scratch. VirtualMars writes the binary only on the compile path, so
    # an untouched file is the hit -- elapsed time is not, because a cold
    # compile is ~1s on a big build host and no ceiling separates the two.
    baked = cached[0].stat().st_mtime_ns
    reload_s = build()
    print(f"cache reload: {reload_s:.2f}s")
    after = sorted(cache_dir.glob("world-*.mjb"))
    if [p.name for p in after] != [cached[0].name] or after[0].stat().st_mtime_ns != baked:
        print(
            "ERROR: the second build rewrote the cache, so the runtime misses it and every session would recompile.",
            file=sys.stderr,
        )
        return 1

    # entrypoint.sh reads this to refuse a render scale the cache was not baked for.
    (cache_dir / SCALE_MARKER).write_text(f"{scale}\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
